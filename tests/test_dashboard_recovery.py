"""Dashboard startup uses durable reconciliation rather than a one-shot pending row."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from horizonx.core.leases import LeaseManager
from horizonx.core.types import (
    AgentConfig,
    AttemptRecord,
    AttemptStatus,
    Run,
    RunStatus,
    Session,
    StrategyConfig,
    Task,
)
from horizonx.dashboard.recovery import reconcile_runs, recover_pending_runs
from horizonx.storage.sqlite import SqliteStore


@pytest.mark.asyncio
async def test_started_legacy_pending_row_is_still_recovered(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    task = Task(
        id="dashboard-recovery",
        name="Dashboard recovery",
        prompt="Resume me",
        strategy=StrategyConfig(kind="single"),
        agent=AgentConfig(type="mock", model="mock"),
    )
    run = Run(
        id="run-dashboard-recovery",
        task=task,
        workspace_path=tmp_path / "workspace",
        status=RunStatus.RUNNING,
    )
    await store.save_run(run)
    await store.save_pending_run(run.id, task.model_dump_json())
    await store.mark_pending_run_started(run.id)
    runtime = AsyncMock()
    runtime.run = AsyncMock(return_value=run)
    try:
        tasks = await recover_pending_runs(
            store, runtime, owner="dashboard-test", lease_ttl_seconds=30
        )
        await asyncio.gather(*tasks)

        runtime.run.assert_awaited_once()
        assert runtime.run.await_args.kwargs["resume_from"] == run.id
        assert await store.list_pending_runs(include_started=True) == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_two_startup_scans_schedule_run_once(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    task = Task(
        id="dashboard-once",
        name="Dashboard once",
        prompt="Run once",
        strategy=StrategyConfig(kind="single"),
        agent=AgentConfig(type="mock", model="mock"),
    )
    run = Run(
        id="run-dashboard-once",
        task=task,
        workspace_path=tmp_path / "workspace",
        status=RunStatus.PENDING,
    )
    await store.save_run(run)
    runtime = AsyncMock()
    runtime.run = AsyncMock(return_value=run)
    try:
        first, second = await asyncio.gather(
            recover_pending_runs(store, runtime, owner="dashboard-a"),
            recover_pending_runs(store, runtime, owner="dashboard-b"),
        )
        await asyncio.gather(*first, *second)

        runtime.run.assert_awaited_once()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_dashboard_passes_durable_provider_resume_context(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    task = Task(
        id="dashboard-provider-resume",
        name="Dashboard provider resume",
        prompt="Continue provider thread",
        strategy=StrategyConfig(kind="single"),
        agent=AgentConfig(type="codex", model="test"),
    )
    run = Run(
        id="run-dashboard-provider-resume",
        task=task,
        workspace_path=tmp_path / "workspace",
        status=RunStatus.RUNNING,
    )
    await store.save_run(run)
    session = Session(id="sess-provider", run_id=run.id, sequence_index=0)
    await store.save_session(session)
    attempt = await store.create_attempt(
        AttemptRecord(
            run_id=run.id,
            session_id=session.id,
            status=AttemptStatus.RUNNING,
            provider="codex",
            model="test",
            workspace_path=run.workspace_path,
            provider_session_id="thread-durable",
        )
    )
    runtime = AsyncMock()
    runtime.run = AsyncMock(return_value=run)
    try:
        tasks = await recover_pending_runs(store, runtime, owner="dashboard-resume")
        await asyncio.gather(*tasks)

        kwargs = runtime.run.await_args.kwargs
        assert kwargs["resume_from"] == run.id
        assert kwargs["resume_provider_session_id"] == "thread-durable"
        assert kwargs["recovery_lineage_id"] == attempt.lineage_id
        assert kwargs["retry_cause"] == "provider_session_available"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_reconciliation_reclaims_lease_left_by_dead_process(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    task = Task(
        id="dashboard-stale-lease",
        name="Dashboard stale lease",
        prompt="Recover after lease expiry",
        strategy=StrategyConfig(kind="single"),
        agent=AgentConfig(type="mock", model="mock"),
    )
    run = Run(
        id="run-dashboard-stale-lease",
        task=task,
        workspace_path=tmp_path / "workspace",
        status=RunStatus.RUNNING,
    )
    await store.save_run(run)
    stale = await LeaseManager(store).acquire(
        f"run:{run.id}", owner="dead-worker", ttl_seconds=0.15
    )
    assert stale is not None
    recovered = asyncio.Event()

    async def finish_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        await store.transition_run(run.id, RunStatus.COMPLETED)
        recovered.set()
        return await store.load_run(run.id)

    runtime = AsyncMock()
    runtime.run = AsyncMock(side_effect=finish_run)
    supervisor = asyncio.create_task(
        reconcile_runs(
            store,
            runtime,
            owner="dashboard-reconciler",
            scan_interval_seconds=0.05,
            lease_ttl_seconds=0.15,
            retry_backoff_seconds=0,
        )
    )
    try:
        await asyncio.wait_for(recovered.wait(), timeout=1)
        runtime.run.assert_awaited_once()
    finally:
        supervisor.cancel()
        with pytest.raises(asyncio.CancelledError):
            await supervisor
        await store.close()
