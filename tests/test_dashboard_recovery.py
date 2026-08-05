"""Dashboard startup uses durable reconciliation rather than a one-shot pending row."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from horizonx.core.leases import LeaseManager
from horizonx.core.operator_commands import OperatorCommand, OperatorCommandKind
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
async def test_restarted_reconciler_resumes_resolved_hitl_and_releases_lease(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    run = Run(
        id="run-hitl-restart",
        task=Task(
            id="hitl-restart",
            name="HITL restart",
            prompt="continue",
            strategy=StrategyConfig(kind="single"),
            agent=AgentConfig(type="mock", model="mock"),
        ),
        workspace_path=tmp_path / "workspace",
        status=RunStatus.PAUSED_HITL,
    )
    await store.save_run(run)
    session = Session(id="sess-hitl", run_id=run.id, sequence_index=0)
    await store.save_session(session)
    attempt = await store.create_attempt(
        AttemptRecord(
            run_id=run.id,
            session_id=session.id,
            status=AttemptStatus.PAUSED_HITL,
            provider="mock",
            model="mock",
            workspace_path=run.workspace_path,
        )
    )
    request_id = await store.save_hitl_event(run.id, "validator_paused", {})
    await store.resolve_hitl_event(
        request_id,
        action="approve",
        actor="alice",
        reason="reviewed",
        instruction="continue",
        idempotency_key="restart-decision",
    )

    async def finish(*args, **kwargs):  # type: ignore[no-untyped-def]
        return await store.transition_run(run.id, RunStatus.COMPLETED)

    runtime = AsyncMock()
    runtime.run = AsyncMock(side_effect=finish)
    try:
        tasks = await recover_pending_runs(
            store,
            runtime,
            owner="replacement",
            retry_backoff_seconds=0,
        )
        await asyncio.gather(*tasks)
        runtime.run.assert_awaited_once()
        assert (await store.load_attempt(attempt.id)).status == AttemptStatus.INTERRUPTED
        assert (await store.load_run(run.id)).status == RunStatus.COMPLETED
        assert await store.get_lease(f"run:{run.id}") is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_restarted_reconciler_consumes_cancel_while_hitl_paused(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    run = Run(
        id="run-hitl-cancel-restart",
        task=Task(
            id="hitl-cancel-restart",
            name="HITL cancel restart",
            prompt="stop",
            strategy=StrategyConfig(kind="single"),
            agent=AgentConfig(type="mock", model="mock"),
        ),
        workspace_path=tmp_path / "workspace",
        status=RunStatus.PAUSED_HITL,
    )
    await store.save_run(run)
    session = Session(id="sess-hitl-cancel", run_id=run.id, sequence_index=0)
    await store.save_session(session)
    attempt = await store.create_attempt(
        AttemptRecord(
            run_id=run.id,
            session_id=session.id,
            status=AttemptStatus.PAUSED_HITL,
            provider="mock",
            model="mock",
            workspace_path=run.workspace_path,
        )
    )
    await store.save_hitl_event(run.id, "validator_paused", {})
    command, _ = await store.create_operator_command(
        OperatorCommand(
            run_id=run.id,
            kind=OperatorCommandKind.CANCEL,
            actor="alice",
            reason="stop",
            idempotency_key="cancel-after-restart",
        )
    )
    runtime = AsyncMock()
    try:
        tasks = await recover_pending_runs(store, runtime, owner="replacement")
        assert tasks == []
        runtime.run.assert_not_awaited()
        assert (await store.load_attempt(attempt.id)).status == AttemptStatus.ABORTED
        assert (await store.load_run(run.id)).status == RunStatus.ABORTED
        consumed = (await store.list_operator_commands(run.id))[0]
        assert consumed.id == command.id and consumed.consumed_at is not None
        assert await store.get_lease(f"run:{run.id}") is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_http_cancel_is_consumed_by_replacement_after_owner_expires(
    tmp_path: Path,
) -> None:
    pytest.importorskip("httpx")
    from httpx import ASGITransport, AsyncClient

    from horizonx.core.event_bus import InMemoryBus
    from horizonx.core.runtime import Runtime
    from horizonx.dashboard.app import create_app

    app = create_app(tmp_path / "horizonx.db", tmp_path / "workspaces")
    app.state.store = SqliteStore(tmp_path / "horizonx.db")
    app.state.bus = InMemoryBus()
    app.state.runtime = Runtime(
        app.state.store, app.state.bus, tmp_path / "workspaces"
    )
    run = Run(
        id="run-http-cancel-recovery",
        task=Task(
            id="http-cancel-recovery",
            name="HTTP cancel recovery",
            prompt="wait",
            strategy=StrategyConfig(kind="single"),
            agent=AgentConfig(type="mock", model="mock"),
        ),
        workspace_path=tmp_path / "workspace",
        status=RunStatus.PAUSED_HITL,
    )
    await app.state.store.save_run(run)
    session = Session(id="sess-http-cancel", run_id=run.id, sequence_index=0)
    await app.state.store.save_session(session)
    await app.state.store.create_attempt(
        AttemptRecord(
            run_id=run.id,
            session_id=session.id,
            status=AttemptStatus.PAUSED_HITL,
            provider="mock",
            model="mock",
            workspace_path=run.workspace_path,
        )
    )
    await app.state.store.save_hitl_event(run.id, "validator_paused", {})
    dead_lease = await LeaseManager(app.state.store).acquire(
        f"run:{run.id}", owner="dead-owner", ttl_seconds=0.05
    )
    assert dead_lease is not None
    runtime = AsyncMock()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/api/runs/{run.id}/cancel",
                headers={
                    "idempotency-key": "http-cancel-after-crash",
                    "x-horizonx-actor": "alice",
                    "x-horizonx-reason": "stop recovered run",
                },
            )
        assert response.json()["status"] == "accepted"
        assert (await app.state.store.load_run(run.id)).status == RunStatus.PAUSED_HITL
        await asyncio.sleep(0.06)
        tasks = await recover_pending_runs(
            app.state.store, runtime, owner="replacement", lease_ttl_seconds=1
        )
        assert tasks == []
        runtime.run.assert_not_awaited()
        hitl = (await app.state.store.list_hitl_events(run.id))[0]
        assert hitl["decision"] == "abort" and hitl["operator"] == "alice"
        assert hitl["reason"] == "stop recovered run"
        assert (await app.state.store.load_run(run.id)).status == RunStatus.ABORTED
        assert (await app.state.store.list_operator_commands(run.id))[0].consumed_at
        assert await app.state.store.get_lease(f"run:{run.id}") is None
    finally:
        await app.state.store.close()


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
