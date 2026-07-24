from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from horizonx.core.event_bus import Event, InMemoryBus
from horizonx.core.types import HITLDecision
from horizonx.storage.sqlite import SqliteStore

from .deps import get_bus, get_store

router = APIRouter()


class HITLResolveBody(BaseModel):
    action: Literal["approve", "modify", "abort", "re_decompose"]
    instruction: str = ""
    operator: str | None = None


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
    store: SqliteStore = Depends(get_store),
    bus: InMemoryBus = Depends(get_bus),
) -> dict[str, str]:
    try:
        run = await store.load_run(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")

    decision = HITLDecision(
        action=body.action,
        instruction=body.instruction,
        operator=body.operator,
    )
    decision_path = Path(run.workspace_path) / ".hitl_decision.json"
    decision_path.write_text(json.dumps(decision.model_dump(mode="json"), default=str))

    # Update the most recent unresolved hitl_event in the DB
    events = await store.list_hitl_events(run_id)
    unresolved = [e for e in events if e.get("resolved_at") is None]
    if unresolved:
        latest_id = unresolved[-1]["id"]
        await store.update_hitl_event(
            latest_id,
            decision.action,
            decision.operator,
            decision.instruction,
        )

    # Publish SSE event so the dashboard updates immediately
    await bus.publish(
        Event(
            type="hitl.resolved",
            run_id=run_id,
            payload={
                "action": decision.action,
                "operator": decision.operator,
                "instruction": decision.instruction,
            },
        )
    )

    return {"status": "written", "path": str(decision_path)}
