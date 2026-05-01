"""HITL gate — pause execution and await operator decision.

Default: console-based interactive prompt. Pluggable for Slack, web, etc.
See docs/LONG_HORIZON_AGENT.md §32.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from horizonx.core.types import HITLConfig, HITLDecision, Run


async def await_decision(
    run: Run, reason: str, context: dict[str, Any], cfg: HITLConfig
) -> HITLDecision:
    """Block until operator decides. Default impl: console prompt."""

    # Print structured context
    sys.stderr.write("\n" + "=" * 70 + "\n")
    sys.stderr.write(f"⚠️  HITL pause — run {run.id}\n")
    sys.stderr.write(f"   reason: {reason}\n")
    sys.stderr.write(f"   context: {json.dumps(context, default=str, indent=2)[:1000]}\n")
    sys.stderr.write("=" * 70 + "\n")

    if cfg.notification_type == "slack":
        await _notify_slack(cfg.notification_target, run.id, reason, context)
    elif cfg.notification_type == "webhook":
        await _notify_webhook(cfg.notification_target, run.id, reason, context)

    # Wait for an operator decision file or interactive input
    decision_path = run.workspace_path / ".hitl_decision.json"

    if not sys.stdin.isatty() and not os.environ.get("HORIZONX_HITL_AUTO_APPROVE"):
        # Wait for decision file
        while not decision_path.exists():
            await asyncio.sleep(2.0)
        data = json.loads(decision_path.read_text())
        decision_path.unlink()
        return HITLDecision(**data)

    if os.environ.get("HORIZONX_HITL_AUTO_APPROVE") == "1":
        return HITLDecision(action="approve", instruction="auto-approved")

    # Interactive console
    sys.stderr.write("Choose action: [a]pprove / [m]odify / [r]e-decompose / [x]abort: ")
    sys.stderr.flush()
    choice = (await asyncio.get_running_loop().run_in_executor(None, input)).strip().lower() or "a"
    action = {"a": "approve", "m": "modify", "r": "re_decompose", "x": "abort"}.get(choice, "approve")
    instruction = ""
    if action == "modify":
        sys.stderr.write("Enter instruction: ")
        sys.stderr.flush()
        instruction = await asyncio.get_running_loop().run_in_executor(None, input)
    return HITLDecision(action=action, instruction=instruction)


async def _notify_slack(channel: str | None, run_id: str, reason: str, ctx: dict) -> None:
    if not channel:
        return
    # Stub. Real impl: from slack_sdk.web.async_client import AsyncWebClient
    sys.stderr.write(f"[slack] would notify {channel} for {run_id}: {reason}\n")


async def _notify_webhook(url: str | None, run_id: str, reason: str, ctx: dict) -> None:
    if not url:
        return
    try:
        import httpx

        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url, json={"run_id": run_id, "reason": reason, "context": ctx})
    except Exception:
        pass
