"""Forced-crash and idempotency contracts for durable recovery."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from horizonx.core.event_bus import Event
from horizonx.core.goal_graph import GoalGraph
from horizonx.core.leases import LeaseLostError, LeaseManager
from horizonx.core.recovery import RecoveryAction, RecoveryCoordinator, RetryPolicy
from horizonx.core.runtime import Runtime
from horizonx.core.types import (
    AgentConfig,
    AttemptRecord,
    AttemptStatus,
    GateAction,
    GateDecision,
    GoalStatus,
    Run,
    RunStatus,
    Session,
    SessionRunResult,
    SessionStatus,
    StrategyConfig,
    Task,
)
from horizonx.storage.sqlite import SqliteStore


def _run(tmp_path: Path, *, agent_type: str = "codex") -> Run:
    return Run(
        id="run-recovery",
        task=Task(
            id="recovery-matrix",
            name="Recovery matrix",
            prompt="Resume safely",
            strategy=StrategyConfig(kind="single"),
            agent=AgentConfig(type=agent_type, model="test-model"),
        ),
        workspace_path=tmp_path / "workspace",
        status=RunStatus.RUNNING,
    )


async def _attempt(
    store: SqliteStore,
    run: Run,
    *,
    status: AttemptStatus = AttemptStatus.RUNNING,
    provider_session_id: str | None = None,
    goal_id: str | None = None,
    retry_count: int = 0,
    max_attempts: int = 3,
) -> AttemptRecord:
    session = Session(
        id="sess-recovery",
        run_id=run.id,
        sequence_index=0,
        target_goal_id=goal_id,
    )
    await store.save_session(session)
    return await store.create_attempt(
        AttemptRecord(
            run_id=run.id,
            session_id=session.id,
            goal_id=goal_id,
            status=status,
            provider=run.task.agent.type,
            model=run.task.agent.model,
            workspace_path=run.workspace_path,
            provider_session_id=provider_session_id,
            retry_count=retry_count,
            max_attempts=max_attempts,
        )
    )


@pytest.mark.asyncio
async def test_events_are_monotonic_append_only_and_idempotent(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    run = _run(tmp_path)
    await store.save_run(run)
    try:
        first = Event(id="event-one", type="run.started", run_id=run.id)
        second = Event(id="event-two", type="session.started", run_id=run.id)

        saved_first = await store.append_event(first)
        saved_duplicate = await store.append_event(first)
        saved_second = await store.append_event(second)

        assert saved_duplicate.sequence == saved_first.sequence
        assert saved_second.sequence > saved_first.sequence
        assert [event.id for event in await store.list_events(run.id)] == [
            "event-one",
            "event-two",
        ]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_runtime_persists_attempt_and_correlated_event_timeline(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store, workspace_root=tmp_path / "workspaces")
    task = Task(
        id="durable-runtime",
        name="Durable runtime",
        prompt="Record one attempt",
        strategy=StrategyConfig(kind="single"),
        agent=AgentConfig(type="mock", model="mock"),
    )
    try:
        run = await runtime.run(task)

        attempts = await store.list_attempts(run.id)
        events = await store.list_events(run.id)
        attempt_events = [event for event in events if event.attempt_id]
        assert len(attempts) == 1
        assert attempts[0].status == AttemptStatus.COMPLETED
        assert attempts[0].provider_session_id == "mock-session-001"
        assert [event.sequence for event in events] == sorted(
            event.sequence for event in events
        )
        assert {event.type for event in attempt_events} >= {
            "attempt.started",
            "step.recorded",
            "attempt.completed",
        }
        assert {event.attempt_id for event in attempt_events} == {attempts[0].id}
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_expired_lease_is_reclaimed_exactly_once(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    leases = LeaseManager(store)
    now = datetime(2026, 8, 2, tzinfo=UTC)
    try:
        original = await leases.acquire(
            "run:one", owner="worker-a", ttl_seconds=10, now=now
        )
        assert original is not None
        assert await leases.acquire(
            "run:one", owner="worker-b", ttl_seconds=10, now=now
        ) is None

        claims = await asyncio.gather(
            leases.acquire(
                "run:one",
                owner="worker-b",
                ttl_seconds=10,
                now=now + timedelta(seconds=11),
            ),
            leases.acquire(
                "run:one",
                owner="worker-c",
                ttl_seconds=10,
                now=now + timedelta(seconds=11),
            ),
        )
        winners = [claim for claim in claims if claim is not None]
        assert len(winners) == 1
        assert winners[0].version == original.version + 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_released_lease_preserves_monotonic_fencing_version(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    leases = LeaseManager(store)
    now = datetime(2026, 8, 2, tzinfo=UTC)
    try:
        first = await leases.acquire(
            "run:one", owner="worker-a", ttl_seconds=10, now=now
        )
        assert first is not None
        assert await store.release_lease(
            first.resource_id, first.owner, first.version
        )
        assert await store.get_lease(first.resource_id) is None

        second = await leases.acquire(
            "run:one",
            owner="worker-b",
            ttl_seconds=10,
            now=now + timedelta(seconds=1),
        )

        assert second is not None
        assert second.version == first.version + 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_lost_lease_cancels_stale_work(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    leases = LeaseManager(store)
    lease = await leases.acquire("run:one", owner="worker-a", ttl_seconds=0.3)
    assert lease is not None

    async def stale_work() -> None:
        async with leases.maintain(lease, ttl_seconds=0.3):
            await asyncio.sleep(2)

    task = asyncio.create_task(stale_work())
    try:
        await asyncio.sleep(0.05)
        assert await store.release_lease(
            lease.resource_id, lease.owner, lease.version
        )
        replacement = await leases.acquire(
            lease.resource_id, owner="worker-b", ttl_seconds=1
        )
        assert replacement is not None

        with pytest.raises(LeaseLostError, match="lease lost"):
            await asyncio.wait_for(task, timeout=1)
    finally:
        if not task.done():
            task.cancel()
        await store.close()


@pytest.mark.asyncio
async def test_crash_before_workspace_preparation_restarts_run(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    run = _run(tmp_path)
    run.status = RunStatus.PENDING
    await store.save_run(run)
    try:
        decisions = await RecoveryCoordinator(store).plan(owner="recovery-worker")

        assert len(decisions) == 1
        assert decisions[0].action == RecoveryAction.RESTART_RUN
        assert decisions[0].reason == "run_has_no_attempt"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_recovery_before_provider_start_creates_new_attempt_plan(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    run = _run(tmp_path)
    await store.save_run(run)
    orphan = await _attempt(store, run)
    try:
        decisions = await RecoveryCoordinator(store).plan(owner="recovery-worker")

        assert len(decisions) == 1
        assert decisions[0].action == RecoveryAction.NEW_ATTEMPT
        assert decisions[0].previous_attempt_id == orphan.id
        assert decisions[0].provider_session_id is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_recovery_resumes_capable_provider_from_durable_session_id(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    run = _run(tmp_path, agent_type="codex")
    await store.save_run(run)
    orphan = await _attempt(
        store, run, provider_session_id="provider-thread-123"
    )
    try:
        decisions = await RecoveryCoordinator(store).plan(owner="recovery-worker")

        assert len(decisions) == 1
        assert decisions[0].action == RecoveryAction.RESUME_PROVIDER
        assert decisions[0].previous_attempt_id == orphan.id
        assert decisions[0].provider_session_id == "provider-thread-123"
        assert decisions[0].lineage_id == orphan.lineage_id
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_runtime_uses_recovery_decision_for_provider_resume(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store, workspace_root=tmp_path / "workspaces")
    run = _run(tmp_path, agent_type="codex")
    await store.save_run(run)
    await runtime.prepare_workspace(run, resume=False)
    await store.save_run(run)
    orphan = await _attempt(
        store, run, provider_session_id="provider-thread-resume"
    )
    decisions = await RecoveryCoordinator(
        store, retry_policy=RetryPolicy(base_backoff_seconds=0)
    ).plan(owner="recovery-worker")
    observed: dict[str, str | None] = {}

    class CapturingAgent:
        async def run_session(
            self, *, resume_session_id=None, **kwargs  # type: ignore[no-untyped-def]
        ) -> SessionRunResult:
            observed["resume_session_id"] = resume_session_id
            return SessionRunResult(
                status=SessionStatus.COMPLETED,
                agent_session_id=resume_session_id,
            )

    try:
        decision = decisions[0]
        with patch(
            "horizonx.core.attempt_executor.build_agent",
            return_value=CapturingAgent(),
        ):
            recovered = await runtime.run(
                run.task,
                resume_from=run.id,
                resume_provider_session_id=decision.provider_session_id,
                recovery_lineage_id=decision.lineage_id,
                retry_cause=decision.reason,
            )

        attempts = await store.list_attempts(run.id)
        assert recovered.status == RunStatus.COMPLETED
        assert observed["resume_session_id"] == "provider-thread-resume"
        assert len(attempts) == 2
        assert attempts[0].status == AttemptStatus.INTERRUPTED
        assert attempts[1].status == AttemptStatus.COMPLETED
        assert attempts[1].lineage_id == orphan.lineage_id
        assert attempts[1].retry_count == 1
    finally:
        decision = decisions[0]
        await store.release_lease(
            decision.lease.resource_id,
            decision.lease.owner,
            decision.lease.version,
        )
        await store.close()


@pytest.mark.asyncio
async def test_recovery_retries_when_adapter_cannot_resume(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    run = _run(tmp_path, agent_type="custom")
    await store.save_run(run)
    await _attempt(store, run, provider_session_id="opaque-provider-id")
    try:
        decisions = await RecoveryCoordinator(store).plan(owner="recovery-worker")

        assert len(decisions) == 1
        assert decisions[0].action == RecoveryAction.NEW_ATTEMPT
        assert decisions[0].provider_session_id is None
        assert decisions[0].reason == "adapter_cannot_resume"
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_type", "extra"),
    [
        ("openhands", {}),
        ("codex", {"ephemeral": True}),
        ("claude_code", {"no_session_persistence": True}),
    ],
)
async def test_recovery_does_not_resume_without_durable_provider_state(
    tmp_path: Path, agent_type: str, extra: dict[str, bool]
) -> None:
    store = SqliteStore(tmp_path / f"{agent_type}.db")
    run = _run(tmp_path, agent_type=agent_type)
    run.task.agent.extra.update(extra)
    await store.save_run(run)
    await _attempt(store, run, provider_session_id="non-durable-provider-id")
    try:
        decisions = await RecoveryCoordinator(store).plan(owner="recovery-worker")

        assert decisions[0].action == RecoveryAction.NEW_ATTEMPT
        assert decisions[0].reason == "adapter_cannot_resume"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_orphaned_goal_claim_is_released_before_retry(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    run = _run(tmp_path)
    await store.save_run(run)
    graph = GoalGraph.empty("Recover goal", "goal interrupted mid-attempt")
    await store.create_graph(run.id, graph)
    orphan = await _attempt(store, run, goal_id="g.root")
    assert await store.claim_goal(run.id, "g.root", orphan.session_id)
    try:
        decisions = await RecoveryCoordinator(store).plan(owner="recovery-worker")

        recovered_goal = await store.load_goal(run.id, "g.root")
        assert decisions[0].action == RecoveryAction.NEW_ATTEMPT
        assert recovered_goal is not None
        assert recovered_goal.status.value == "pending"
        assert recovered_goal.assigned_to_session is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_recovery_does_not_reopen_completed_goal(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    run = _run(tmp_path)
    await store.save_run(run)
    graph = GoalGraph.empty("Completed goal", "must remain completed")
    await store.create_graph(run.id, graph)
    orphan = await _attempt(store, run, goal_id="g.root")
    assert await store.claim_goal(run.id, "g.root", orphan.session_id)
    await store.transition_goal(
        run.id,
        "g.root",
        expected_version=1,
        to_status=GoalStatus.DONE,
        session_id=orphan.session_id,
    )
    try:
        decisions = await RecoveryCoordinator(store).plan(owner="recovery-worker")

        completed_goal = await store.load_goal(run.id, "g.root")
        assert decisions[0].action == RecoveryAction.NEW_ATTEMPT
        assert completed_goal is not None
        assert completed_goal.status.value == "done"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_hitl_pause_is_not_relaunched(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    run = _run(tmp_path)
    run.status = RunStatus.PAUSED_HITL
    await store.save_run(run)
    await _attempt(store, run, status=AttemptStatus.PAUSED_HITL)
    try:
        decisions = await RecoveryCoordinator(store).plan(owner="recovery-worker")

        assert decisions == []
        assert await store.get_lease(f"run:{run.id}") is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_retry_limit_terminates_orphan_instead_of_relaunching(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    run = _run(tmp_path)
    await store.save_run(run)
    session = Session(id="sess-exhausted", run_id=run.id, sequence_index=0)
    await store.save_session(session)
    await store.create_attempt(
        AttemptRecord(
            run_id=run.id,
            session_id=session.id,
            status=AttemptStatus.RUNNING,
            provider=run.task.agent.type,
            model=run.task.agent.model,
            workspace_path=run.workspace_path,
            max_attempts=1,
        )
    )
    try:
        decisions = await RecoveryCoordinator(store).plan(owner="recovery-worker")

        assert decisions == []
        assert (await store.load_run(run.id)).status == RunStatus.FAILED
        assert await store.get_lease(f"run:{run.id}") is None
        events = await store.list_events(run.id, event_type="recovery.planned")
        assert events[-1].payload["reason"] == "retry_limit_exhausted"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_completed_attempt_requires_reconciliation_instead_of_reexecution(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    run = _run(tmp_path)
    await store.save_run(run)
    completed = await _attempt(
        store,
        run,
        status=AttemptStatus.COMPLETED,
        provider_session_id="already-finished-provider-session",
    )
    try:
        decisions = await RecoveryCoordinator(store).plan(owner="recovery-worker")

        assert decisions == []
        assert (await store.load_run(run.id)).status == RunStatus.PAUSED_HITL
        assert await store.get_lease(f"run:{run.id}") is None
        events = await store.list_events(run.id, event_type="recovery.planned")
        assert events[-1].attempt_id == completed.id
        assert events[-1].payload["action"] == "pause_for_reconciliation"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_terminal_failed_attempt_still_enforces_retry_limit(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    run = _run(tmp_path)
    await store.save_run(run)
    await _attempt(
        store,
        run,
        status=AttemptStatus.FAILED,
        provider_session_id="failed-provider-session",
        max_attempts=1,
    )
    try:
        decisions = await RecoveryCoordinator(store).plan(owner="recovery-worker")

        assert decisions == []
        assert (await store.load_run(run.id)).status == RunStatus.FAILED
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_failed_attempt_retries_without_resuming_failed_provider_session(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    run = _run(tmp_path)
    await store.save_run(run)
    await _attempt(
        store,
        run,
        status=AttemptStatus.FAILED,
        provider_session_id="failed-provider-session",
    )
    try:
        decisions = await RecoveryCoordinator(store).plan(owner="recovery-worker")

        assert decisions[0].action == RecoveryAction.NEW_ATTEMPT
        assert decisions[0].provider_session_id is None
        assert decisions[0].reason == "attempt_failed"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_aborted_attempt_is_not_relaunched(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    run = _run(tmp_path)
    await store.save_run(run)
    await _attempt(store, run, status=AttemptStatus.ABORTED)
    try:
        decisions = await RecoveryCoordinator(store).plan(owner="recovery-worker")

        assert decisions == []
        assert (await store.load_run(run.id)).status == RunStatus.ABORTED
        assert await store.get_lease(f"run:{run.id}") is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_second_recovery_pass_cannot_duplicate_plan(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    run = _run(tmp_path)
    await store.save_run(run)
    await _attempt(store, run)
    coordinator = RecoveryCoordinator(store)
    try:
        first, second = await asyncio.gather(
            coordinator.plan(owner="worker-a"),
            coordinator.plan(owner="worker-b"),
        )

        assert sorted((len(first), len(second))) == [0, 1]
        events = await store.list_events(run.id, event_type="recovery.planned")
        assert len(events) == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_recovery_is_replanned_after_reconciler_crash_and_lease_expiry(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    run = _run(tmp_path)
    await store.save_run(run)
    orphan = await _attempt(store, run)
    now = datetime(2026, 8, 2, tzinfo=UTC)
    coordinator = RecoveryCoordinator(
        store,
        lease_ttl_seconds=10,
        retry_policy=RetryPolicy(base_backoff_seconds=0),
    )
    try:
        first = await coordinator.plan(owner="reconciler-a", now=now)
        second = await coordinator.plan(
            owner="reconciler-b", now=now + timedelta(seconds=11)
        )

        assert len(first) == len(second) == 1
        assert first[0].lineage_id == second[0].lineage_id == orphan.lineage_id
        assert second[0].lease.version == first[0].lease.version + 1
        events = await store.list_events(run.id, event_type="recovery.planned")
        assert len(events) == 2
        assert events[0].id != events[1].id
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_recovery_planning_error_releases_lease(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    run = _run(tmp_path)
    await store.save_run(run)
    await _attempt(store, run)
    store.append_event = AsyncMock(side_effect=RuntimeError("event write failed"))  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="event write failed"):
            await RecoveryCoordinator(store).plan(owner="recovery-worker")

        assert await store.get_lease(f"run:{run.id}") is None
    finally:
        await store.close()


def test_provider_session_survives_forced_mid_stream_process_kill(
    tmp_path: Path,
) -> None:
    database = tmp_path / "forced-kill.db"
    workspace = tmp_path / "forced-workspace"
    project_root = Path(__file__).parents[1]
    script = textwrap.dedent(
        f"""
        import asyncio
        import os
        from pathlib import Path
        from unittest.mock import patch

        from horizonx.core.attempt_executor import AttemptExecutor
        from horizonx.core.runtime import Runtime
        from horizonx.core.types import (
            AgentConfig, Run, RunStatus, SessionRunResult, SessionStatus,
            Step, StepType, StrategyConfig, Task,
        )
        from horizonx.storage.sqlite import SqliteStore

        class CrashAgent:
            async def run_session(self, *, on_step, **kwargs):
                await on_step(Step(
                    session_id="stream",
                    sequence=0,
                    type=StepType.SESSION_ID,
                    content={{"session_id": "provider-before-kill"}},
                ))
                os._exit(23)

        async def main():
            store = SqliteStore(Path({str(database)!r}))
            runtime = Runtime(store, workspace_root=Path({str(tmp_path / 'managed')!r}))
            task = Task(
                id="forced-kill",
                name="Forced kill",
                prompt="crash after provider init",
                strategy=StrategyConfig(kind="single"),
                agent=AgentConfig(type="codex", model="test"),
            )
            run = Run(
                id="run-forced-kill",
                task=task,
                workspace_path=Path({str(workspace)!r}),
                status=RunStatus.RUNNING,
            )
            run.workspace_path.mkdir(parents=True)
            await store.save_run(run)
            with patch("horizonx.core.attempt_executor.build_agent", return_value=CrashAgent()):
                await AttemptExecutor(runtime).execute(run, prompt="start")

        asyncio.run(main())
        """
    )
    child_env = dict(os.environ)
    child_env["PYTHONPATH"] = os.pathsep.join(
        [str(project_root), child_env.get("PYTHONPATH", "")]
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        env=child_env,
        check=False,
        timeout=15,
    )
    assert completed.returncode == 23

    async def inspect_recovery() -> None:
        store = SqliteStore(database)
        try:
            attempts = await store.list_attempts("run-forced-kill")
            assert len(attempts) == 1
            assert attempts[0].status == AttemptStatus.RUNNING
            assert attempts[0].provider_session_id == "provider-before-kill"

            decisions = await RecoveryCoordinator(store).plan(owner="replacement")
            assert decisions[0].action == RecoveryAction.RESUME_PROVIDER
            assert decisions[0].provider_session_id == "provider-before-kill"
        finally:
            await store.close()

    asyncio.run(inspect_recovery())


@pytest.mark.asyncio
async def test_recovery_lineage_deduplicates_validator_evidence(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    run = _run(tmp_path)
    await store.save_run(run)
    first = await _attempt(store, run)
    decision = GateDecision(
        decision=GateAction.CONTINUE,
        reason="tests passed",
        validator_name="test-suite",
    )
    first_session = (await store.list_sessions(run.id))[0]
    second_session = Session(
        id="sess-recovered",
        run_id=run.id,
        sequence_index=1,
    )
    await store.save_session(second_session)
    await store.create_attempt(
        AttemptRecord(
            lineage_id=first.lineage_id,
            run_id=run.id,
            session_id=second_session.id,
            status=AttemptStatus.RUNNING,
            provider=run.task.agent.type,
            model=run.task.agent.model,
            workspace_path=run.workspace_path,
            retry_cause="provider_session_available",
        )
    )
    try:
        await store.save_validation(run, first_session, decision)
        await store.save_validation(run, first_session, decision)
        await store.save_validation(run, second_session, decision)

        validations = await store.list_validations(run.id)
        assert len(validations) == 1
        assert validations[0]["idempotency_key"].startswith(first.lineage_id or "")
    finally:
        await store.close()
