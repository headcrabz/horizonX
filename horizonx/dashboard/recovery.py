"""Recover pending dashboard-launched runs after process restart."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def recover_pending_runs(store: Any, runtime: Any) -> None:
    """Re-attach any runs that were launched via the dashboard but not yet completed."""
    pending = await store.list_pending_runs()
    if not pending:
        return
    logger.info("Recovering %d pending run(s) from previous session", len(pending))
    for row in pending:
        run_id = row["run_id"]
        task_json = row["task_json"]
        try:
            from horizonx.core.types import Task
            task = Task.model_validate_json(task_json)
            await store.mark_pending_run_started(run_id)
            asyncio.create_task(
                _run_and_cleanup(runtime, task, run_id, store),
                name=f"recover-{run_id}",
            )
            logger.info("Recovered run %s", run_id)
        except Exception as exc:
            logger.error("Failed to recover run %s: %s", run_id, exc)


async def _run_and_cleanup(runtime: Any, task: Any, run_id: str, store: Any) -> None:
    try:
        await runtime.run(task, resume_from=run_id)
    finally:
        await store.delete_pending_run(run_id)
