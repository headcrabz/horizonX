"""End-to-end resource-governance invariants."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from horizonx.agents.mock import MockAgent
from horizonx.core.attempt_executor import AttemptExecutor
from horizonx.core.event_bus import InMemoryBus
from horizonx.core.governor import BudgetExceeded, ResourceGovernor
from horizonx.core.runtime import Runtime
from horizonx.core.types import (
    AgentConfig,
    CumulativeMetrics,
    ResourceLimits,
    Run,
    Session,
    SessionRunResult,
    SessionStatus,
    Step,
    StepType,
    StrategyConfig,
    Task,
    WorkspaceConfig,
    utcnow,
)
from horizonx.storage.sqlite import SqliteStore


def _run(run_id: str, workspace: Path) -> Run:
    return Run(
        id=run_id,
        task=Task(
            id=f"task-{run_id}",
            name=f"Task {run_id}",
            prompt="Exercise resource governance",
            strategy=StrategyConfig(kind="single"),
            agent=AgentConfig(type="mock", model="mock"),
            resources=ResourceLimits(max_total_tokens=1_000),
        ),
        workspace_path=workspace,
    )


def test_unknown_cost_is_not_represented_as_zero() -> None:
    assert SessionRunResult(status=SessionStatus.COMPLETED).cost_usd is None
    assert CumulativeMetrics().usd is None


@pytest.mark.asyncio
async def test_cache_usage_is_charged_and_persisted(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    run = _run("run-cache", tmp_path / "workspace")
    result = SessionRunResult(
        status=SessionStatus.COMPLETED,
        tokens_in=100,
        tokens_out=20,
        cache_creation_tokens=30,
        cache_read_tokens=50,
        cost_usd=0.1,
    )
    await store.save_run(run)

    try:
        async with runtime._governor(run):
            await runtime.charge(run, result)

        persisted = await store.load_run(run.id)
        assert persisted.cumulative.cache_creation_tokens == 30
        assert persisted.cumulative.cache_read_tokens == 50
        assert persisted.cumulative.cache_hit_rate == pytest.approx(1 / 3)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_concurrent_runs_charge_only_their_own_governor(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    first = _run("run-first", tmp_path / "first")
    second = _run("run-second", tmp_path / "second")
    result = SessionRunResult(
        status=SessionStatus.COMPLETED,
        tokens_in=30,
        tokens_out=20,
        cost_usd=0.25,
    )

    try:
        async with runtime._governor(first), runtime._governor(second):
            await runtime.charge(first, result)

        assert first.cumulative.tokens_in == 30
        assert first.cumulative.tokens_out == 20
        assert first.cumulative.usd == pytest.approx(0.25)
        assert second.cumulative.tokens_in == 0
        assert second.cumulative.tokens_out == 0
        assert second.cumulative.usd is None
    finally:
        await store.close()


class _DelayedUsageStore:
    def __init__(self) -> None:
        self.recorded = False

    async def record(self, *args: object) -> None:
        self.recorded = True


@pytest.mark.asyncio
async def test_charge_waits_for_durable_usage_write(tmp_path: Path) -> None:
    run = _run("run-durable-usage", tmp_path / "workspace")
    run.task.workspace = WorkspaceConfig(workspace_id="shared")
    usage_store = _DelayedUsageStore()
    governor = ResourceGovernor(
        run.task.resources,
        run,
        InMemoryBus(),
        usage_store=usage_store,
    )

    async with governor:
        await governor.charge(tokens_in=10, tokens_out=5, usd=0.1)

    assert usage_store.recorded is True


@pytest.mark.asyncio
async def test_charge_enforces_workspace_daily_budget_after_recording(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    run = _run("run-daily-budget", tmp_path / "workspace")
    run.task.workspace = WorkspaceConfig(
        workspace_id="shared-daily",
        daily_budget_usd=0.15,
    )
    await store.save_run(run)
    result = SessionRunResult(
        status=SessionStatus.COMPLETED,
        tokens_in=10,
        cost_usd=0.2,
    )

    try:
        async with runtime._governor(run):
            with pytest.raises(BudgetExceeded, match="daily budget"):
                await runtime.charge(run, result)
        assert await store.workspace_daily_usd("shared-daily") == pytest.approx(0.2)
    finally:
        await store.close()


class _UsageAgent:
    async def run_session(self, *, on_step, session_id, **kwargs):  # type: ignore[no-untyped-def]
        await on_step(
            Step(
                session_id=session_id,
                sequence=0,
                type=StepType.THOUGHT,
                content={"text": "working"},
            )
        )
        return SessionRunResult(
            status=SessionStatus.COMPLETED,
            tokens_in=40,
            tokens_out=30,
            cost_usd=0.2,
        )


class _SilentAgent:
    async def run_session(self, **kwargs):  # type: ignore[no-untyped-def]
        import asyncio

        await asyncio.sleep(60)
        return SessionRunResult(status=SessionStatus.COMPLETED)


class _StreamingUsageAgent:
    def __init__(self) -> None:
        self.was_cancelled = False

    async def run_session(self, *, on_step, cancel_token, session_id, **kwargs):  # type: ignore[no-untyped-def]
        await on_step(
            Step(
                session_id=session_id,
                sequence=0,
                type=StepType.USAGE,
                content={"input_tokens": 60, "output_tokens": 5},
            )
        )
        if cancel_token.cancelled:
            self.was_cancelled = True
            return SessionRunResult(
                status=SessionStatus.TIMEOUT,
                error=cancel_token.reason,
                tokens_in=60,
                tokens_out=5,
            )
        return SessionRunResult(status=SessionStatus.COMPLETED)


class _ClaudeLikeUsageAgent:
    async def run_session(self, *, on_step, cancel_token, session_id, **kwargs):  # type: ignore[no-untyped-def]
        for sequence, source, mode in (
            (0, "assistant", "delta"),
            (1, "result", "cumulative"),
        ):
            await on_step(
                Step(
                    session_id=session_id,
                    sequence=sequence,
                    type=StepType.USAGE,
                    content={
                        "usage": {"input_tokens": 500, "output_tokens": 100},
                        "source": source,
                        "usage_mode": mode,
                    },
                )
            )
        return SessionRunResult(
            status=(
                SessionStatus.TIMEOUT
                if cancel_token.cancelled
                else SessionStatus.COMPLETED
            ),
            error=cancel_token.reason or None,
            tokens_in=500,
            tokens_out=100,
        )


@pytest.mark.asyncio
async def test_session_limit_blocks_a_second_attempt_before_launch(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    run = _run("run-session-limit", tmp_path / "workspace")
    run.task.resources.max_sessions = 1
    run.workspace_path.mkdir()
    await store.save_run(run)
    try:
        with patch(
            "horizonx.core.attempt_executor.build_agent", return_value=MockAgent()
        ):
            await AttemptExecutor(runtime).execute(run, prompt="first")
            with pytest.raises(BudgetExceeded, match="session limit"):
                await AttemptExecutor(runtime).execute(run, prompt="second")

        assert len(await store.list_sessions(run.id)) == 1
        assert len(await store.list_attempts(run.id)) == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_session_limit_is_atomic_for_concurrent_attempts(tmp_path: Path) -> None:
    import asyncio

    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    run = _run("run-atomic-session-limit", tmp_path / "workspace")
    run.task.resources.max_sessions = 1
    await store.save_run(run)

    try:
        results = await asyncio.gather(
            runtime.start_session(run),
            runtime.start_session(run),
            return_exceptions=True,
        )
        assert sum(not isinstance(result, Exception) for result in results) == 1
        assert sum(isinstance(result, BudgetExceeded) for result in results) == 1
        assert len(await store.list_sessions(run.id)) == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_per_session_token_limit_persists_usage_before_stopping(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    run = _run("run-token-limit", tmp_path / "workspace")
    run.task.resources.max_tokens_per_session = 50
    run.workspace_path.mkdir()
    await store.save_run(run)

    try:
        with patch(
            "horizonx.core.attempt_executor.build_agent", return_value=_UsageAgent()
        ):
            async with runtime._governor(run):
                with pytest.raises(BudgetExceeded, match="session token limit"):
                    await AttemptExecutor(runtime).execute(run, prompt="bounded")

        persisted_session = (await store.list_sessions(run.id))[0]
        persisted_run = await store.load_run(run.id)
        assert persisted_session.tokens_used == 70
        assert persisted_run.cumulative.tokens_in == 40
        assert persisted_run.cumulative.tokens_out == 30
        assert persisted_run.cumulative.steps_count == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_streamed_usage_stops_session_at_token_boundary(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    run = _run("run-stream-token-limit", tmp_path / "workspace")
    run.task.resources.max_tokens_per_session = 50
    run.workspace_path.mkdir()
    await store.save_run(run)
    agent = _StreamingUsageAgent()

    try:
        with patch(
            "horizonx.core.attempt_executor.build_agent",
            return_value=agent,
        ):
            with pytest.raises(BudgetExceeded, match="session token limit"):
                await AttemptExecutor(runtime).execute(run, prompt="bounded")

        assert agent.was_cancelled is True
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_cumulative_terminal_usage_replaces_claude_stream_deltas(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    run = _run("run-claude-usage", tmp_path / "workspace")
    run.task.resources.max_tokens_per_session = 1_000
    run.workspace_path.mkdir()
    await store.save_run(run)

    try:
        with patch(
            "horizonx.core.attempt_executor.build_agent",
            return_value=_ClaudeLikeUsageAgent(),
        ):
            result = await AttemptExecutor(runtime).execute(run, prompt="bounded")

        assert result.status == SessionStatus.COMPLETED
        assert result.session.tokens_used == 600
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_watchdog_nudges_then_classifies_a_hard_stall(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    run = _run("run-stall", tmp_path / "workspace")
    run.task.resources.stall_soft_seconds = 0.01
    run.task.resources.stall_hard_seconds = 0.03
    run.task.resources.max_minutes_per_session = 0.002
    run.workspace_path.mkdir()
    await store.save_run(run)

    try:
        with patch(
            "horizonx.core.attempt_executor.build_agent", return_value=_SilentAgent()
        ):
            result = await AttemptExecutor(runtime).execute(run, prompt="stall")

        events = await store.list_events(run_id=run.id)
        assert result.status == SessionStatus.TIMEOUT
        assert result.agent.error == "stall_hard_timeout"
        assert any(event.type == "session.stall_nudge" for event in events)
        assert any(event.type == "session.stall_abort" for event in events)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_workspace_concurrency_limit_rejects_overlapping_runs(
    tmp_path: Path,
) -> None:
    import asyncio

    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    task = _run("template", tmp_path / "unused").task
    task.workspace = WorkspaceConfig(workspace_id="shared", max_concurrent_runs=1)
    task.agent.extra["delay_per_step"] = 0.05

    try:
        first = asyncio.create_task(runtime.run(task))
        await asyncio.sleep(0.02)
        with pytest.raises(BudgetExceeded, match="concurrent run limit"):
            await runtime.run(task)
        await first
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_restart_reconciles_durable_session_step_counts(tmp_path: Path) -> None:
    database = tmp_path / "horizonx.db"
    store = SqliteStore(database)
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    run = _run("run-reconcile", tmp_path / "workspace")
    await store.save_run(run)
    session = Session(
        run_id=run.id,
        sequence_index=0,
        started_at=utcnow() - timedelta(seconds=10),
    )
    await store.save_session(session)
    await runtime.record_step(
        session,
        Step(
            session_id=session.id,
            sequence=0,
            type=StepType.THOUGHT,
            content={"text": "durable work"},
        ),
    )
    await runtime.record_step(
        session,
        Step(
            session_id=session.id,
            sequence=1,
            type=StepType.TOOL_CALL,
            tool_name="Bash",
            content={"command": "git commit -m checkpoint"},
        ),
    )
    await store.close()

    reopened = SqliteStore(database)
    resumed_runtime = Runtime(
        store=reopened, workspace_root=tmp_path / "reopened-workspaces"
    )
    try:
        resumed = await resumed_runtime._load_or_create(run.task, run.id)
        assert resumed.cumulative.sessions_count == 1
        assert resumed.cumulative.steps_count == 1
        assert resumed.cumulative.housekeeping_steps == 1
        assert resumed.cumulative.wall_seconds >= 9.9
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_restart_reconciles_usage_from_durable_charge_events(
    tmp_path: Path,
) -> None:
    database = tmp_path / "horizonx.db"
    store = SqliteStore(database)
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    run = _run("run-usage-reconcile", tmp_path / "workspace")
    await store.save_run(run)
    result = SessionRunResult(
        status=SessionStatus.COMPLETED,
        tokens_in=100,
        tokens_out=25,
        cache_read_tokens=40,
        cost_usd=0.3,
    )
    async with runtime._governor(run):
        await runtime.charge(run, result)
    run.cumulative = CumulativeMetrics()
    await store.save_run(run)
    await store.close()

    reopened = SqliteStore(database)
    resumed_runtime = Runtime(
        store=reopened, workspace_root=tmp_path / "reopened-workspaces"
    )
    try:
        resumed = await resumed_runtime._load_or_create(run.task, run.id)
        assert resumed.cumulative.tokens_in == 100
        assert resumed.cumulative.tokens_out == 25
        assert resumed.cumulative.cache_read_tokens == 40
        assert resumed.cumulative.usd == pytest.approx(0.3)
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_restart_reconciles_partial_streamed_usage_without_final_charge(
    tmp_path: Path,
) -> None:
    database = tmp_path / "horizonx.db"
    store = SqliteStore(database)
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    run = _run("run-partial-usage", tmp_path / "workspace")
    await store.save_run(run)
    session = await runtime.start_session(run)
    await runtime.record_step(
        session,
        Step(
            session_id=session.id,
            sequence=0,
            type=StepType.USAGE,
            content={
                "usage": {"input_tokens": 80, "output_tokens": 20},
                "usage_mode": "delta",
            },
        ),
    )
    await runtime.record_step(
        session,
        Step(
            session_id=session.id,
            sequence=1,
            type=StepType.USAGE,
            content={
                "usage": {"input_tokens": 80, "output_tokens": 20},
                "usage_mode": "cumulative",
            },
        ),
    )
    await store.close()

    reopened = SqliteStore(database)
    resumed_runtime = Runtime(
        store=reopened, workspace_root=tmp_path / "reopened-workspaces"
    )
    try:
        resumed = await resumed_runtime._load_or_create(run.task, run.id)
        assert resumed.cumulative.tokens_in == 80
        assert resumed.cumulative.tokens_out == 20
    finally:
        await reopened.close()
