from __future__ import annotations

import asyncio
import os
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ValidationError

from horizonx.core.leases import LeaseManager
from horizonx.core.runtime import Runtime
from horizonx.core.types import Run, RunStatus
from horizonx.storage.sqlite import SqliteStore

from .deps import get_runtime, get_store

router = APIRouter()


class LaunchBody(BaseModel):
    task: dict[str, Any]


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
    asyncio.create_task(_run_and_cleanup(run.id), name=f"run-{run.id}")

    return {"run_id": run.id}


@router.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    store: SqliteStore = Depends(get_store),
) -> dict[str, str]:
    """Best-effort cancel: sets status to ABORTED in the DB.

    The running coroutine may not stop immediately — no in-flight cancel token.
    """
    try:
        run = await store.load_run(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"run {run_id!r} not found") from None
    transitioned = await store.transition_run(run.id, RunStatus.ABORTED)
    if transitioned.status != RunStatus.ABORTED:
        raise HTTPException(
            status_code=409,
            detail=f"run is already terminal: {transitioned.status.value}",
        )
    return {"status": transitioned.status.value, "run_id": run_id}
