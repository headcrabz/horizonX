from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from horizonx.core.event_bus import Event, InMemoryBus
from horizonx.core.operator_commands import OperatorCommand, OperatorCommandKind
from horizonx.core.types import HITLDecision
from horizonx.hitl.slack_interactions import verify_slack_signature
from horizonx.storage.sqlite import SqliteStore

from .deps import get_bus, get_store

router = APIRouter()


class HITLResolveBody(BaseModel):
    action: Literal["approve", "modify", "abort", "re_decompose"]
    instruction: str = ""
    operator: str | None = None
    reason: str = ""
    request_id: str | None = None
    idempotency_key: str | None = None


def _authenticate_operator(request: Request) -> None:
    expected = os.environ.get("HORIZONX_OPERATOR_TOKEN")
    if expected is None:
        return
    supplied = request.headers.get("authorization", "")
    if supplied != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="invalid operator credentials")


@router.get("/runs/{run_id}/hitl")
async def list_hitl(
    run_id: str,
    store: SqliteStore = Depends(get_store),
) -> list[dict[str, Any]]:
    return await store.list_hitl_events(run_id)


@router.post("/runs/{run_id}/hitl")
async def resolve_hitl(
    run_id: str,
    body: HITLResolveBody,
    request: Request,
    store: SqliteStore = Depends(get_store),
    bus: InMemoryBus = Depends(get_bus),
) -> dict[str, str]:
    _authenticate_operator(request)
    try:
        run = await store.load_run(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"run {run_id!r} not found") from None

    decision = HITLDecision(
        action=body.action,
        instruction=body.instruction,
        operator=body.operator,
    )
    events = await store.list_hitl_events(run_id)
    unresolved = [e for e in events if e.get("resolved_at") is None]
    if body.request_id is not None and not any(
        event["id"] == body.request_id for event in events
    ):
        raise HTTPException(
            status_code=404,
            detail=f"HITL request {body.request_id!r} not found for run {run_id!r}",
        )
    request_id = body.request_id or (unresolved[-1]["id"] if unresolved else None)
    if request_id is None:
        request_id = await store.save_hitl_event(
            run_id,
            "dashboard_resolution",
            {},
            actor=body.operator or "dashboard-operator",
            instruction=body.instruction,
        )
    idempotency_key = (
        body.idempotency_key
        or request.headers.get("idempotency-key")
        or f"web:{uuid4().hex}"
    )
    command, _ = await store.create_operator_command(
        OperatorCommand(
            run_id=run_id,
            kind=OperatorCommandKind.DECISION,
            actor=decision.operator or "dashboard-operator",
            reason=body.reason,
            instruction=decision.instruction,
            payload={"request_id": request_id, "action": decision.action},
            idempotency_key=idempotency_key,
        )
    )
    resolved, changed = await store.resolve_hitl_event(
        request_id,
        action=decision.action,
        actor=command.actor,
        reason=command.reason,
        instruction=command.instruction,
        idempotency_key=command.idempotency_key,
    )

    # Compatibility projection for older local CLI waiters; SQLite is authoritative.
    decision_path = Path(run.workspace_path) / ".hitl_decision.json"
    decision_path.write_text(json.dumps(decision.model_dump(mode="json"), default=str))

    # Publish SSE event so the dashboard updates immediately
    if changed:
        await bus.publish(
            Event(
                type="hitl.resolved",
                run_id=run_id,
                payload={
                    "request_id": request_id,
                    "action": resolved["decision"],
                    "operator": resolved["operator"],
                    "instruction": resolved["instruction"],
                },
            )
        )

    return {"status": "resolved", "path": str(decision_path), "request_id": request_id}


def _slack_instruction(payload: dict[str, Any]) -> str:
    values = payload.get("view", {}).get("state", {}).get("values", {})
    for block in values.values():
        for field in block.values():
            value = field.get("value")
            if isinstance(value, str):
                return value
    return ""


@router.post("/hitl/slack/interactions")
async def slack_interaction(
    request: Request,
    store: SqliteStore = Depends(get_store),
    bus: InMemoryBus = Depends(get_bus),
) -> dict[str, str]:
    body = await request.body()
    secret = os.environ.get("HORIZONX_SLACK_SIGNING_SECRET")
    if not secret:
        raise HTTPException(status_code=503, detail="Slack signing secret is not configured")
    try:
        verify_slack_signature(
            body=body,
            timestamp=request.headers.get("x-slack-request-timestamp", ""),
            signature=request.headers.get("x-slack-signature", ""),
            signing_secret=secret,
        )
        encoded = parse_qs(body.decode()).get("payload", [""])[0]
        payload = json.loads(encoded)
        action = payload["actions"][0]
        request_id, decision = action["value"].rsplit(":", 1)
        if decision not in {"approve", "modify", "abort"}:
            raise ValueError("unsupported Slack action")
        events = await store.find_hitl_event(request_id)
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from None
    user = payload.get("user", {})
    actor = str(user.get("id") or user.get("username") or "slack-operator")
    callback_key = str(
        payload.get("trigger_id")
        or f"{payload.get('team', {}).get('id', '')}:"
        f"{payload.get('container', {}).get('message_ts', '')}:"
        f"{action.get('action_id', '')}:{actor}"
    )
    command, _ = await store.create_operator_command(
        OperatorCommand(
            run_id=events["run_id"],
            kind=OperatorCommandKind.DECISION,
            actor=actor,
            reason="Slack interaction",
            instruction=_slack_instruction(payload),
            payload={"request_id": request_id, "action": decision},
            idempotency_key=f"slack:{callback_key}",
        )
    )
    resolved, changed = await store.resolve_hitl_event(
        request_id,
        action=decision,
        actor=command.actor,
        reason=command.reason,
        instruction=command.instruction,
        idempotency_key=command.idempotency_key,
    )
    if changed:
        await bus.publish(
            Event(
                type="hitl.resolved",
                run_id=resolved["run_id"],
                payload={"request_id": request_id, "action": resolved["decision"]},
            )
        )
    return {"status": "resolved" if changed else "duplicate", "request_id": request_id}
