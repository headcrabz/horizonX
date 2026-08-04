"""Contract tests for the shared bounded-attempt lifecycle."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from horizonx.agents.mock import MockAgent
from horizonx.core.attempt_executor import AttemptExecutor
from horizonx.core.governor import BudgetExceeded
from horizonx.core.runtime import Runtime
from horizonx.core.spin_detector import SpinReport
from horizonx.core.types import (
    AgentConfig,
    AttemptStatus,
    GateAction,
    GateDecision,
    GoalNode,
    ResourceLimits,
    Run,
    RunStatus,
    SessionRunResult,
    SessionStatus,
    Step,
    StepType,
    StrategyConfig,
    Task,
)
from horizonx.storage.sqlite import SqliteStore


def _run(tmp_path: Path) -> Run:
    return Run(
        id="run-attempt",
        task=Task(
            id="attempt-contract",
            name="Attempt contract",
            prompt="Exercise one attempt",
            strategy=StrategyConfig(kind="single"),
            agent=AgentConfig(type="mock", model="mock"),
        ),
        workspace_path=tmp_path,
        status=RunStatus.RUNNING,
    )


@pytest.mark.asyncio
async def test_success_uses_one_persisted_lifecycle(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    run = _run(tmp_path / "workspace")
    run.workspace_path.mkdir()
    await store.save_run(run)
    decision = GateDecision(
        decision=GateAction.CONTINUE,
        reason="passed",
        validator_name="contract-check",
    )
    runtime.run_validators = AsyncMock(return_value=[decision])  # type: ignore[method-assign]
    runtime.charge = AsyncMock()  # type: ignore[method-assign]
    cleanup = AsyncMock()
    try:
        with patch(
            "horizonx.core.attempt_executor.build_agent", return_value=MockAgent()
        ):
            result = await AttemptExecutor(runtime).execute(
                run,
                prompt="Do the work",
                validator_stages=("after_every_session",),
                cleanup=(cleanup,),
            )

        sessions = await store.list_sessions(run.id)
        steps = await store.recent_steps(result.session.id, 20)
        assert result.succeeded is True
        assert result.attempt.status == AttemptStatus.COMPLETED
        assert result.decisions == [decision]
        assert len(sessions) == 1
        assert sessions[0].status == SessionStatus.COMPLETED
        assert len(steps) == 3
        runtime.charge.assert_awaited_once()
        cleanup.assert_awaited_once()
    finally:
        await store.close()


class _RaisingAgent:
    async def run_session(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("transport broke")


class _CancelledAgent:
    async def run_session(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise asyncio.CancelledError


class _BlockingAgent:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def run_session(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.started.set()
        await asyncio.sleep(60)
        return SessionRunResult(status=SessionStatus.COMPLETED)


class _SessionIdAgent:
    def __init__(self, store: SqliteStore, run_id: str) -> None:
        self.store = store
        self.run_id = run_id
        self.persisted_during_stream = False

    async def run_session(self, *, on_step, **kwargs):  # type: ignore[no-untyped-def]
        await on_step(
            Step(
                session_id="placeholder",
                sequence=0,
                type=StepType.SESSION_ID,
                content={"session_id": "provider-session-live"},
            )
        )
        session = (await self.store.list_sessions(self.run_id))[0]
        self.persisted_during_stream = (
            session.agent_session_id == "provider-session-live"
        )
        return SessionRunResult(status=SessionStatus.COMPLETED)


class _LegacyProtocolAgent:
    """External agent written against the protocol before session_id was added."""

    def __init__(self) -> None:
        self.called = False

    async def run_session(
        self,
        session_prompt,  # type: ignore[no-untyped-def]
        workspace,  # type: ignore[no-untyped-def]
        *,
        resume_session_id=None,  # type: ignore[no-untyped-def]
        on_step=None,  # type: ignore[no-untyped-def]
        cancel_token=None,  # type: ignore[no-untyped-def]
    ) -> SessionRunResult:
        self.called = True
        return SessionRunResult(status=SessionStatus.COMPLETED)


@pytest.mark.asyncio
async def test_exception_becomes_errored_and_always_cleans_up(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    run = _run(tmp_path / "workspace")
    run.workspace_path.mkdir()
    await store.save_run(run)
    cleanup = AsyncMock()
    try:
        with patch(
            "horizonx.core.attempt_executor.build_agent", return_value=_RaisingAgent()
        ):
            result = await AttemptExecutor(runtime).execute(
                run, prompt="Do the work", cleanup=(cleanup,)
            )

        persisted = (await store.list_sessions(run.id))[0]
        assert result.status == SessionStatus.ERRORED
        assert result.agent.error == "transport broke"
        assert persisted.status == SessionStatus.ERRORED
        cleanup.assert_awaited_once()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_task_cancellation_propagates_after_cleanup(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    run = _run(tmp_path / "workspace")
    run.workspace_path.mkdir()
    await store.save_run(run)
    cleanup = AsyncMock()
    try:
        with patch(
            "horizonx.core.attempt_executor.build_agent", return_value=_CancelledAgent()
        ):
            with pytest.raises(asyncio.CancelledError):
                await AttemptExecutor(runtime).execute(
                    run, prompt="Do the work", cleanup=(cleanup,)
                )

        assert (await store.list_sessions(run.id))[0].status == SessionStatus.ERRORED
        cleanup.assert_awaited_once()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_external_cancellation_is_not_reclassified_as_a_stall(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    run = _run(tmp_path / "workspace")
    run.workspace_path.mkdir()
    await store.save_run(run)
    cleanup = AsyncMock()
    agent = _BlockingAgent()
    try:
        with patch(
            "horizonx.core.attempt_executor.build_agent",
            return_value=agent,
        ):
            execution = asyncio.create_task(
                AttemptExecutor(runtime).execute(
                    run, prompt="block", cleanup=(cleanup,)
                )
            )
            await agent.started.wait()
            execution.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(execution, timeout=0.2)

        cleanup.assert_awaited_once()
        assert (await store.list_sessions(run.id))[0].status == SessionStatus.ERRORED
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_agent_construction_failure_still_terminates_and_cleans_up(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    run = _run(tmp_path / "workspace")
    run.workspace_path.mkdir()
    await store.save_run(run)
    cleanup = AsyncMock()
    try:
        with patch(
            "horizonx.core.attempt_executor.build_agent",
            side_effect=ValueError("agent plugin is unavailable"),
        ):
            result = await AttemptExecutor(runtime).execute(
                run, prompt="Do the work", cleanup=(cleanup,)
            )

        assert result.status == SessionStatus.ERRORED
        assert (await store.list_sessions(run.id))[0].status == SessionStatus.ERRORED
        cleanup.assert_awaited_once()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_budget_exception_propagates_after_session_cleanup(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    run = _run(tmp_path / "workspace")
    run.workspace_path.mkdir()
    await store.save_run(run)
    runtime.charge = MagicMock(  # type: ignore[method-assign]
        side_effect=BudgetExceeded("run budget exhausted")
    )
    cleanup = AsyncMock()
    try:
        with patch(
            "horizonx.core.attempt_executor.build_agent", return_value=MockAgent()
        ):
            with pytest.raises(BudgetExceeded, match="run budget exhausted"):
                await AttemptExecutor(runtime).execute(
                    run, prompt="Do the work", cleanup=(cleanup,)
                )

        assert (await store.list_sessions(run.id))[0].status == SessionStatus.COMPLETED
        cleanup.assert_awaited_once()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_provider_session_id_is_persisted_during_stream(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    run = _run(tmp_path / "workspace")
    run.workspace_path.mkdir()
    await store.save_run(run)
    agent = _SessionIdAgent(store, run.id)
    try:
        with patch(
            "horizonx.core.attempt_executor.build_agent", return_value=agent
        ):
            result = await AttemptExecutor(runtime).execute(run, prompt="Do the work")

        assert result.succeeded is True
        assert agent.persisted_during_stream is True
        assert result.session.agent_session_id == "provider-session-live"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_external_agent_without_session_id_keyword_remains_compatible(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    run = _run(tmp_path / "workspace")
    run.workspace_path.mkdir()
    await store.save_run(run)
    agent = _LegacyProtocolAgent()
    try:
        with patch(
            "horizonx.core.attempt_executor.build_agent", return_value=agent
        ):
            result = await AttemptExecutor(runtime).execute(run, prompt="Do the work")

        assert agent.called is True
        assert result.succeeded is True
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_timeout_has_one_terminal_session_and_cleanup(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    run = _run(tmp_path / "workspace")
    run.workspace_path.mkdir()
    await store.save_run(run)
    cleanup = AsyncMock()
    slow_agent = MockAgent(delay_per_step=0.1)
    try:
        with patch(
            "horizonx.core.attempt_executor.build_agent", return_value=slow_agent
        ):
            result = await AttemptExecutor(runtime).execute(
                run,
                prompt="Do the work",
                timeout_seconds=0.01,
                cleanup=(cleanup,),
            )

        persisted = (await store.list_sessions(run.id))[0]
        assert result.status == SessionStatus.TIMEOUT
        assert persisted.status == SessionStatus.TIMEOUT
        cleanup.assert_awaited_once()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_cleanup_failure_prevents_false_completion(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    run = _run(tmp_path / "workspace")
    run.workspace_path.mkdir()
    await store.save_run(run)
    cleanup = AsyncMock(side_effect=RuntimeError("temporary directory is busy"))
    try:
        with patch(
            "horizonx.core.attempt_executor.build_agent", return_value=MockAgent()
        ):
            result = await AttemptExecutor(runtime).execute(
                run, prompt="Do the work", cleanup=(cleanup,)
            )

        assert result.status == SessionStatus.ERRORED
        assert result.agent.error == "cleanup failed: temporary directory is busy"
        assert (await store.list_sessions(run.id))[0].status == SessionStatus.ERRORED
    finally:
        await store.close()


class _RepeatingAgent:
    async def run_session(self, *, on_step, cancel_token, **kwargs):  # type: ignore[no-untyped-def]
        for sequence in range(6):
            await on_step(
                Step(
                    session_id="placeholder",
                    sequence=sequence,
                    type=StepType.TOOL_CALL,
                    tool_name="shell",
                    content={"command": "retry"},
                )
            )
            if cancel_token.cancelled:
                return SessionRunResult(status=SessionStatus.TIMEOUT)
        return SessionRunResult(status=SessionStatus.COMPLETED)


@pytest.mark.asyncio
async def test_step_limit_cancels_attempt_for_every_strategy_path(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    run = _run(tmp_path / "workspace")
    run.task.resources = ResourceLimits(max_steps_per_session=2)
    run.workspace_path.mkdir()
    await store.save_run(run)
    try:
        with patch(
            "horizonx.core.attempt_executor.build_agent", return_value=_RepeatingAgent()
        ):
            result = await AttemptExecutor(runtime).execute(run, prompt="Do the work")

        assert result.status == SessionStatus.TIMEOUT
        assert result.agent.error == "session_step_limit"
        assert result.session.steps_count == 2
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_hard_spin_detection_has_a_distinct_terminal_status(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    run = _run(tmp_path / "workspace")
    run.workspace_path.mkdir()
    await store.save_run(run)
    runtime.check_spin = AsyncMock(  # type: ignore[method-assign]
        return_value=SpinReport(
            detected=True,
            layer="within_session",
            score=1.0,
            evidence=["same command repeated"],
            action="terminate_session_and_retry",
        )
    )
    try:
        with patch(
            "horizonx.core.attempt_executor.build_agent", return_value=_RepeatingAgent()
        ):
            result = await AttemptExecutor(runtime).execute(run, prompt="Do the work")

        assert result.status == SessionStatus.SPIN
        assert result.spin_detected is True
        assert (await store.list_sessions(run.id))[0].status == SessionStatus.SPIN
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_soft_spin_warning_does_not_mark_attempt_as_failed(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    run = _run(tmp_path / "workspace")
    run.workspace_path.mkdir()
    await store.save_run(run)
    runtime.check_spin = AsyncMock(  # type: ignore[method-assign]
        return_value=SpinReport(
            detected=True,
            layer="within_session",
            score=0.8,
            evidence=["possible repetition"],
            action="warn_and_inject_diagnostic",
        )
    )
    try:
        with patch(
            "horizonx.core.attempt_executor.build_agent", return_value=_RepeatingAgent()
        ):
            result = await AttemptExecutor(runtime).execute(run, prompt="Do the work")

        assert result.status == SessionStatus.COMPLETED
        assert result.spin_detected is False
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_attempt_uses_goal_retry_limit(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    run = _run(tmp_path / "workspace")
    run.workspace_path.mkdir()
    await store.save_run(run)
    goal = GoalNode(
        id="g.retry",
        name="Retryable goal",
        description="Carry its policy into durable state",
        max_attempts=7,
    )

    try:
        result = await AttemptExecutor(runtime).execute(
            run,
            prompt="complete the goal",
            target_goal=goal,
        )

        assert result.attempt.max_attempts == 7
    finally:
        await store.close()
