"""Reconcile durable non-terminal runs after dashboard process restart."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any
from uuid import uuid4

from horizonx.core.leases import LeaseManager
from horizonx.core.recovery import RecoveryAction, RecoveryCoordinator, RecoveryDecision

logger = logging.getLogger(__name__)


async def reconcile_runs(
    store: Any,
    runtime: Any,
    *,
    owner: str | None = None,
    scan_interval_seconds: float = 10.0,
    lease_ttl_seconds: float = 30.0,
    retry_backoff_seconds: float = 1.0,
) -> None:
    """Continuously reclaim orphaned runs, including leases that expire after boot."""
    if scan_interval_seconds <= 0:
        raise ValueError("scan_interval_seconds must be positive")
    recovery_owner = owner or f"dashboard-{os.getpid()}-{uuid4().hex[:8]}"
    active: set[asyncio.Task[None]] = set()

    def task_finished(task: asyncio.Task[None]) -> None:
        active.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error("Recovered run failed", exc_info=error)

    try:
        while True:
            tasks = await recover_pending_runs(
                store,
                runtime,
                owner=recovery_owner,
                lease_ttl_seconds=lease_ttl_seconds,
                retry_backoff_seconds=retry_backoff_seconds,
            )
            for task in tasks:
                active.add(task)
                task.add_done_callback(task_finished)
            await asyncio.sleep(scan_interval_seconds)
    finally:
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)


async def recover_pending_runs(
    store: Any,
    runtime: Any,
    *,
    owner: str | None = None,
    lease_ttl_seconds: float = 30.0,
    retry_backoff_seconds: float = 1.0,
) -> list[asyncio.Task[None]]:
    """Lease and schedule every recoverable non-terminal run exactly once."""
    recovery_owner = owner or f"dashboard-{os.getpid()}-{uuid4().hex[:8]}"
    from horizonx.core.recovery import RetryPolicy

    coordinator = RecoveryCoordinator(
        store,
        lease_ttl_seconds=lease_ttl_seconds,
        retry_policy=RetryPolicy(base_backoff_seconds=retry_backoff_seconds),
    )
    decisions = await coordinator.plan(owner=recovery_owner)
    tasks: list[asyncio.Task[None]] = []
    for decision in decisions:
        task = asyncio.create_task(
            _run_and_cleanup(
                runtime,
                decision,
                store,
                lease_ttl_seconds=lease_ttl_seconds,
            ),
            name=f"recover-{decision.run_id}",
        )
        tasks.append(task)
        logger.info(
            "Recovery planned for %s: %s", decision.run_id, decision.action.value
        )
    return tasks


async def _run_and_cleanup(
    runtime: Any,
    decision: RecoveryDecision,
    store: Any,
    *,
    lease_ttl_seconds: float,
) -> None:
    leases = LeaseManager(store)
    async with leases.maintain(
        decision.lease, ttl_seconds=lease_ttl_seconds
    ):
        run = await store.load_run(decision.run_id)
        if decision.not_before is not None:
            from datetime import UTC, datetime

            delay = (decision.not_before - datetime.now(UTC)).total_seconds()
            if delay > 0:
                await asyncio.sleep(delay)
        kwargs: dict[str, Any] = {"resume_from": run.id}
        if decision.action == RecoveryAction.RESUME_PROVIDER:
            kwargs.update(
                resume_provider_session_id=decision.provider_session_id,
                recovery_lineage_id=decision.lineage_id,
                retry_cause=decision.reason,
            )
        elif decision.action == RecoveryAction.NEW_ATTEMPT:
            kwargs.update(
                recovery_lineage_id=decision.lineage_id,
                retry_cause=decision.reason,
            )
        try:
            await runtime.run(run.task, **kwargs)
        finally:
            await store.delete_pending_run(run.id)
