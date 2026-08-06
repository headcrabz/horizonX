from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from horizonx.core.event_bus import InMemoryBus
from horizonx.core.operator_commands import OperatorCommand, OperatorCommandKind
from horizonx.core.types import HITLDecision
from horizonx.hitl.slack_interactions import verify_slack_signature
from horizonx.storage.sqlite import (
    HITLTransitionError,
    OperatorCommandConflict,
    SqliteStore,
)

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
    supplied_idempotency_key = body.idempotency_key or request.headers.get(
        "idempotency-key"
    )
    request_id = body.request_id or run.active_hitl_request_id
    if request_id is None:
        raise HTTPException(status_code=409, detail="run has no active HITL request")
    if request_id != run.active_hitl_request_id:
        raise HTTPException(
            status_code=409,
            detail="HITL request does not own the run's current pause",
        )
    try:
        matching = await store.find_active_hitl_event(run_id, request_id)
    except (HITLTransitionError, KeyError):
        raise HTTPException(
            status_code=409, detail="active HITL request is missing"
        ) from None
    if matching["run_id"] != run_id:
        raise HTTPException(status_code=409, detail="active HITL request has wrong owner")
    if matching["resolved_at"] is not None:
        actor = decision.operator or "dashboard-operator"
        if (
            supplied_idempotency_key is not None
            and matching["resolution_idempotency_key"] == supplied_idempotency_key
            and matching["decision"] == decision.action
            and matching["operator"] == actor
            and (matching["reason"] or "") == body.reason
            and (matching["instruction"] or "") == decision.instruction
        ):
            decision_path = Path(run.workspace_path) / ".hitl_decision.json"
            return {
                "status": "duplicate",
                "path": str(decision_path),
                "request_id": request_id,
            }
        raise HTTPException(status_code=409, detail="HITL request is already resolved")
    idempotency_key = (
        supplied_idempotency_key or f"web:{uuid4().hex}"
    )
    try:
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
    except OperatorCommandConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    try:
        resolved, changed, persisted_event = (
            await store.resolve_active_hitl_event_and_event(
                run_id,
                request_id,
                action=decision.action,
                actor=command.actor,
                reason=command.reason,
                instruction=command.instruction,
                idempotency_key=command.idempotency_key,
            )
        )
    except HITLTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None

    # Compatibility projection for older local CLI waiters; SQLite is authoritative.
    decision_path = Path(run.workspace_path) / ".hitl_decision.json"
    decision_path.write_text(json.dumps(decision.model_dump(mode="json"), default=str))

    # Publish SSE event so the dashboard updates immediately
    if changed:
        await bus.publish(persisted_event)

    return {"status": "resolved", "path": str(decision_path), "request_id": request_id}


def _slack_instruction(payload: dict[str, Any]) -> str:
    values = payload.get("view", {}).get("state", {}).get("values", {})
    for block in values.values():
        for field in block.values():
            value = field.get("value")
            if isinstance(value, str):
                return value
    return ""


async def _open_slack_modify_modal(trigger_id: str, request_id: str) -> None:
    import httpx

    token = _slack_modal_token()
    if not token:
        raise HTTPException(status_code=503, detail="Slack bot token is not configured")
    view = {
        "type": "modal",
        "callback_id": "hitl_modify_submission",
        "private_metadata": request_id,
        "title": {"type": "plain_text", "text": "Modify instruction"},
        "submit": {"type": "plain_text", "text": "Submit"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [{
            "type": "input", "block_id": "modify_instruction",
            "label": {"type": "plain_text", "text": "Instruction"},
            "element": {"type": "plain_text_input", "action_id": "instruction"},
        }],
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://slack.com/api/views.open",
            headers={"authorization": f"Bearer {token}"},
            json={"trigger_id": trigger_id, "view": view},
        )
    data = response.json()
    if not response.is_success or not data.get("ok"):
        raise HTTPException(status_code=502, detail="Slack rejected the modify modal")


def _slack_modal_token() -> str | None:
    """Prefer the modal-specific override, retaining the deployed Slack token."""
    return os.environ.get("HORIZONX_SLACK_BOT_TOKEN") or os.environ.get(
        "HORIZONX_SLACK_TOKEN"
    )


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
        payload_type = payload.get("type", "block_actions")
        if payload_type == "view_submission":
            view = payload["view"]
            if view.get("callback_id") != "hitl_modify_submission":
                raise ValueError("unsupported Slack modal")
            request_id = str(view.get("private_metadata") or "")
            decision = "modify"
            action: dict[str, Any] = {}
        else:
            action = payload["actions"][0]
            request_id, decision = action["value"].rsplit(":", 1)
        if decision not in {"approve", "modify", "abort"}:
            raise ValueError("unsupported Slack action")
        events = await store.find_hitl_event(request_id)
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from None
    user = payload.get("user", {})
    actor = str(user.get("id") or user.get("username") or "slack-operator")
    try:
        events = await store.find_active_hitl_event(events["run_id"], request_id)
    except HITLTransitionError:
        raise HTTPException(
            status_code=409,
            detail="HITL request does not own the run's current pause",
        ) from None
    if payload_type != "view_submission" and decision == "modify":
        if events["resolved_at"] is not None:
            raise HTTPException(status_code=409, detail="HITL request is already resolved")
        trigger_id = str(payload.get("trigger_id") or "")
        if not trigger_id:
            raise HTTPException(status_code=400, detail="Slack trigger_id is required")
        await _open_slack_modify_modal(trigger_id, request_id)
        return {"status": "modal_opened", "request_id": request_id}

    instruction = _slack_instruction(payload).strip()
    if payload_type == "view_submission" and not instruction:
        raise HTTPException(status_code=422, detail="modify instruction must not be empty")
    callback_key = str(
        payload.get("view", {}).get("id")
        or payload.get("trigger_id")
        or f"{payload.get('team', {}).get('id', '')}:"
        f"{payload.get('container', {}).get('message_ts', '')}:"
        f"{action.get('action_id', '')}:{actor}"
    )
    command_key = f"slack:{callback_key}"
    if events["resolved_at"] is not None:
        if (
            events["resolution_idempotency_key"] == command_key
            and events["decision"] == decision
            and events["operator"] == actor
            and (events["instruction"] or "") == instruction
        ):
            return {"status": "duplicate", "request_id": request_id}
        raise HTTPException(status_code=409, detail="HITL request is already resolved")
    try:
        command, _ = await store.create_operator_command(
            OperatorCommand(
                run_id=events["run_id"],
                kind=OperatorCommandKind.DECISION,
                actor=actor,
                reason="Slack interaction",
                instruction=instruction,
                payload={"request_id": request_id, "action": decision},
                idempotency_key=command_key,
            )
        )
    except OperatorCommandConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    try:
        resolved, changed, persisted_event = (
            await store.resolve_active_hitl_event_and_event(
                events["run_id"],
                request_id,
                action=decision,
                actor=command.actor,
                reason=command.reason,
                instruction=command.instruction,
                idempotency_key=command.idempotency_key,
            )
        )
    except HITLTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    if changed:
        await bus.publish(persisted_event)
    return {"status": "resolved" if changed else "duplicate", "request_id": request_id}
