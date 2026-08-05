from __future__ import annotations

import os
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ValidationError

from horizonx.core.leases import LeaseManager
from horizonx.core.operator_commands import OperatorCommand, OperatorCommandKind
from horizonx.core.runtime import Runtime
from horizonx.core.types import TERMINAL_RUN_STATUSES, Run, RunStatus
from horizonx.storage.sqlite import OperatorCommandConflict, SqliteStore

from .deps import get_runtime, get_store
from .routes_hitl import _authenticate_operator

router = APIRouter()


class LaunchBody(BaseModel):
    task: dict[str, Any]


class OperatorCommandBody(BaseModel):
    kind: Literal["cancel", "steer"]
    reason: str = ""
    instruction: str = ""


@router.post("/runs", status_code=202)
async def launch_run(
    body: LaunchBody,
    runtime: Runtime = Depends(get_runtime),
    store: SqliteStore = Depends(get_store),
) -> dict[str, str]:
    from horizonx.core.types import Task

    try:
        task = Task.model_validate(body.task)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    # Pre-create the run record so we can return the run_id immediately
    run = Run(
        task=task,
        workspace_path=runtime.workspace_root,
        status=RunStatus.PENDING,
    )
    run.workspace_path = runtime.workspace_root / run.id
    await store.save_run(run)

    # Persist the pending run for crash recovery before firing the background task
    await store.save_pending_run(run.id, task.model_dump_json())

    lease_ttl_seconds = 30.0
    leases = LeaseManager(store)
    lease = await leases.acquire(
        f"run:{run.id}",
        owner=f"dashboard-launch-{os.getpid()}-{uuid4().hex[:8]}",
        ttl_seconds=lease_ttl_seconds,
    )
    if lease is None:  # pragma: no cover - a new run ID cannot already be leased
        raise HTTPException(status_code=409, detail="run launch lease is unavailable")

    async def _run_and_cleanup(run_id: str) -> None:
        async with leases.maintain(lease, ttl_seconds=lease_ttl_seconds):
            try:
                await runtime.run(task, resume_from=run_id)
            finally:
                await store.delete_pending_run(run_id)

    # Fire strategy execution in background; resume_from causes runtime to reload
    # the pre-created run from the store and start it
    runtime.start_background_run(
        run.id, _run_and_cleanup(run.id), name=f"run-{run.id}"
    )

    return {"run_id": run.id}


@router.post("/runs/{run_id}/commands", status_code=202)
async def submit_operator_command(
    run_id: str,
    body: OperatorCommandBody,
    request: Request,
    runtime: Runtime = Depends(get_runtime),
    store: SqliteStore = Depends(get_store),
) -> dict[str, str]:
    _authenticate_operator(request)
    try:
        run = await store.load_run(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"run {run_id!r} not found") from None
    idempotency_key = (
        request.headers.get("idempotency-key") or f"{body.kind}:{run.id}:{uuid4().hex}"
    )
    candidate = OperatorCommand(
        run_id=run_id,
        kind=OperatorCommandKind(body.kind),
        actor=request.headers.get("x-horizonx-actor", "dashboard-operator"),
        reason=body.reason,
        instruction=body.instruction,
        idempotency_key=idempotency_key,
    )
    try:
        existing = await store.get_operator_command(run.id, idempotency_key)
        if existing is not None:
            command, _ = await store.create_operator_command(candidate)
            return {"status": "duplicate", "command_id": command.id}
        if run.status in TERMINAL_RUN_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=f"run is already terminal: {run.status.value}",
            )
        command, created = await store.create_operator_command(candidate)
    except OperatorCommandConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    runtime.notify_operator_command(run_id)
    return {
        "status": "accepted" if created else "duplicate",
        "command_id": command.id,
    }


@router.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    request: Request,
    runtime: Runtime = Depends(get_runtime),
    store: SqliteStore = Depends(get_store),
) -> dict[str, str]:
    """Persist cancellation before monotonically aborting the run."""
    _authenticate_operator(request)
    try:
        run = await store.load_run(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"run {run_id!r} not found") from None
    idempotency_key = request.headers.get("idempotency-key") or f"cancel:{run.id}:{uuid4().hex}"
    candidate = OperatorCommand(
        run_id=run.id,
        kind=OperatorCommandKind.CANCEL,
        actor=request.headers.get("x-horizonx-actor", "dashboard-operator"),
        reason=request.headers.get("x-horizonx-reason", "operator requested cancellation"),
        idempotency_key=idempotency_key,
    )
    try:
        existing = await store.get_operator_command(run.id, idempotency_key)
        if existing is not None:
            command, _ = await store.create_operator_command(candidate)
            return {"status": "duplicate", "run_id": run_id, "command_id": command.id}
        if run.status in TERMINAL_RUN_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=f"run is already terminal: {run.status.value}",
            )
        command, _ = await store.create_operator_command(candidate)
    except OperatorCommandConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    runtime.notify_operator_command(run_id)
    return {
        "status": "accepted",
        "run_id": run_id,
        "command_id": command.id,
    }
