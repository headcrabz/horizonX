"""HITL gate — pause execution and await operator decision.

Default: console-based interactive prompt. Pluggable for Slack, web, etc.
See docs/LONG_HORIZON_AGENT.md §32.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any, Literal, cast

from horizonx.core.operator_commands import OperatorCommand, OperatorCommandKind
from horizonx.core.types import (
    HITLConfig,
    HITLDecision,
    Run,
)
from horizonx.storage.sqlite import HITLTransitionError, OperatorCommandConflict


async def await_decision(
    run: Run,
    reason: str,
    context: dict[str, Any],
    cfg: HITLConfig,
    *,
    store: Any | None = None,
    request_id: str | None = None,
) -> HITLDecision:
    """Block until operator decides. Default impl: console prompt."""

    # Print structured context
    sys.stderr.write("\n" + "=" * 70 + "\n")
    sys.stderr.write(f"HITL pause — run {run.id}\n")
    sys.stderr.write(f"   reason: {reason}\n")
    sys.stderr.write(f"   context: {json.dumps(context, default=str, indent=2)[:1000]}\n")
    sys.stderr.write("=" * 70 + "\n")

    if cfg.notification_type == "slack":
        await _notify_slack(
            cfg.notification_target,
            run.id,
            reason,
            context,
            cfg,
            request_id=request_id,
        )
    elif cfg.notification_type == "webhook":
        await _notify_webhook(cfg.notification_target, run.id, reason, context)

    # Wait for an operator decision file or interactive input
    decision_path = run.workspace_path / ".hitl_decision.json"

    if store is not None and request_id is not None:
        start_time = time.monotonic()
        timeout_secs = (cfg.timeout_minutes or 0) * 60
        while True:
            persisted_run = await store.load_run(run.id)
            if persisted_run.active_hitl_request_id != request_id:
                if persisted_run.status.value == "aborted":
                    return HITLDecision(
                        action="abort",
                        instruction="run was cancelled while awaiting HITL",
                        operator="system:operator-control",
                    )
                raise RuntimeError(
                    f"HITL request is no longer active: {request_id}"
                )
            commands = await store.list_operator_commands(
                run.id, unconsumed_only=True
            )
            cancel = next(
                (
                    command
                    for command in commands
                    if command.kind == OperatorCommandKind.CANCEL
                ),
                None,
            )
            if cancel is not None:
                await store.apply_cancel_command(cancel.id)
                return HITLDecision(
                    action="abort",
                    instruction=cancel.reason,
                    operator=cancel.actor,
                )
            events = await store.list_hitl_events(run.id)
            request = next((item for item in events if item["id"] == request_id), None)
            if request is None:
                raise RuntimeError(f"HITL request disappeared: {request_id}")
            if request["resolved_at"] is not None:
                result = await store.load_authoritative_active_hitl_decision(
                    run.id, request_id
                )
                await store.consume_operator_command(result.command.id)
                request = result.request
                return HITLDecision(
                    action=request["decision"],
                    instruction=request["instruction"] or "",
                    operator=request["operator"],
                    decided_at=request["resolved_at"],
                )
            if timeout_secs and (time.monotonic() - start_time) >= timeout_secs:
                if cfg.escalation_channel:
                    await _notify_slack(
                        cfg.escalation_channel,
                        run.id,
                        f"TIMEOUT: {reason}",
                        context,
                        cfg,
                        request_id=request_id,
                    )
                action = "abort" if cfg.require_acknowledgement else (
                    cfg.escalation_action or "approve"
                )
                actor = "system:timeout"
                instruction = f"auto-{action} after {cfg.timeout_minutes}m timeout"
                try:
                    result = await store.submit_active_hitl_decision(
                        OperatorCommand(
                            run_id=run.id,
                            kind=OperatorCommandKind.DECISION,
                            actor=actor,
                            reason="HITL timeout",
                            instruction=instruction,
                            payload={"request_id": request_id, "action": action},
                            idempotency_key=f"timeout:{request_id}",
                        )
                    )
                except (OperatorCommandConflict, HITLTransitionError) as exc:
                    persisted_run = await store.load_run(run.id)
                    if persisted_run.active_hitl_request_id != request_id:
                        if persisted_run.status.value == "aborted":
                            return HITLDecision(
                                action="abort",
                                instruction="run was cancelled while awaiting HITL",
                                operator="system:operator-control",
                            )
                        raise RuntimeError(
                            f"HITL request is no longer active: {request_id}"
                        ) from exc
                    try:
                        result = (
                            await store.load_authoritative_active_hitl_decision(
                                run.id, request_id
                            )
                        )
                    except OperatorCommandConflict as adoption_error:
                        persisted_run = await store.load_run(run.id)
                        if persisted_run.status.value == "aborted":
                            return HITLDecision(
                                action="abort",
                                instruction="run was cancelled while awaiting HITL",
                                operator="system:operator-control",
                            )
                        raise RuntimeError(
                            "HITL timeout lost submission without an "
                            "authoritative same-generation decision"
                        ) from adoption_error
                await store.consume_operator_command(result.command.id)
                resolved = result.request
                return HITLDecision(
                    action=resolved["decision"],
                    instruction=resolved["instruction"],
                    operator=resolved["operator"],
                    decided_at=resolved["resolved_at"],
                )
            await asyncio.sleep(0.05)

    if not sys.stdin.isatty() and not os.environ.get("HORIZONX_HITL_AUTO_APPROVE"):
        # Wait for decision file, with optional timeout escalation
        start_time = time.monotonic()
        timeout_secs = (cfg.timeout_minutes or 0) * 60

        while not decision_path.exists():
            await asyncio.sleep(2.0)
            if timeout_secs and (time.monotonic() - start_time) >= timeout_secs:
                # Escalate to secondary channel if configured
                if cfg.escalation_channel:
                    await _notify_slack(
                        cfg.escalation_channel,
                        run.id,
                        f"TIMEOUT: {reason}",
                        context,
                        cfg,
                    )
                action = cfg.escalation_action or "approve"
                return HITLDecision(
                    action=action,
                    instruction=f"auto-{action} after {cfg.timeout_minutes}m timeout",
                )

        data = json.loads(decision_path.read_text())
        decision_path.unlink()
        return HITLDecision(**data)

    if os.environ.get("HORIZONX_HITL_AUTO_APPROVE") == "1":
        return HITLDecision(action="approve", instruction="auto-approved")

    # Interactive console
    sys.stderr.write("Choose action: [a]pprove / [m]odify / [r]e-decompose / [x]abort: ")
    sys.stderr.flush()
    choice = (await asyncio.get_running_loop().run_in_executor(None, input)).strip().lower() or "a"
    action = cast(
        Literal["approve", "abort"],
        {"a": "approve", "m": "modify", "r": "re_decompose", "x": "abort"}.get(choice, "approve"),
    )
    instruction = ""
    if action == "modify":  # type: ignore[comparison-overlap]
        sys.stderr.write("Enter instruction: ")
        sys.stderr.flush()
        instruction = await asyncio.get_running_loop().run_in_executor(None, input)
    return HITLDecision(action=action, instruction=instruction)


async def _notify_slack(
    channel: str | None,
    run_id: str,
    reason: str,
    ctx: dict[str, Any],
    cfg: HITLConfig,
    request_id: str | None = None,
) -> None:
    token = os.environ.get("HORIZONX_SLACK_TOKEN")
    if not token or not channel:
        return
    try:
        from slack_sdk.web.async_client import AsyncWebClient

        client = AsyncWebClient(token=token)
        await client.chat_postMessage(
            channel=channel,
            text=f"HorizonX HITL pause — {reason}",
            blocks=[
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": f"Agent paused: {reason}"},
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Run:* `{run_id}`\n*Reason:* {reason}",
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"```{json.dumps(ctx, default=str)[:500]}```",
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Approve"},
                            "action_id": "hitl_approve",
                            "value": f"{request_id or run_id}:approve",
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Modify"},
                            "action_id": "hitl_modify",
                            "value": f"{request_id or run_id}:modify",
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Abort"},
                            "style": "danger",
                            "action_id": "hitl_abort",
                            "value": f"{request_id or run_id}:abort",
                        },
                    ],
                },
            ],
        )
    except ImportError:
        sys.stderr.write("[hitl] slack_sdk not installed — pip install horizonx[slack]\n")
    except Exception as e:
        sys.stderr.write(f"[hitl] slack notification failed: {e}\n")


async def _notify_webhook(
    url: str | None,
    run_id: str,
    reason: str,
    ctx: dict[str, Any],
) -> None:
    if not url:
        return
    payload = {"run_id": run_id, "reason": reason, "context": ctx}
    for attempt, delay in enumerate([0, 5, 15], start=1):
        if delay:
            await asyncio.sleep(delay)
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code < 500:
                    return
                sys.stderr.write(
                    f"[hitl] webhook attempt {attempt} got {resp.status_code}\n"
                )
        except Exception as e:
            sys.stderr.write(f"[hitl] webhook attempt {attempt} failed: {e}\n")
    sys.stderr.write(f"[hitl] webhook {url} failed after 3 attempts\n")
