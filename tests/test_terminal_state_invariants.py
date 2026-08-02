"""End-to-end invariants for run terminal states."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from horizonx.agents.mock import MockAgent
from horizonx.core.governor import BudgetExceeded
from horizonx.core.runtime import Runtime
from horizonx.core.types import (
    AgentConfig,
    GateAction,
    GateDecision,
    GoalStatus,
    Run,
    RunStatus,
    StrategyConfig,
    StrategyOutcome,
    Task,
)
from horizonx.storage.sqlite import SqliteStore


def _task() -> Task:
    return Task(
        id="terminal-contract",
        name="Terminal contract",
        prompt="Exercise the terminal contract",
        strategy=StrategyConfig(kind="single"),
        agent=AgentConfig(type="mock", model="mock"),
    )


class _FailedStrategy:
    def __init__(self, config: dict[str, object]) -> None:
        pass

    async def execute(self, run: Run, rt: Runtime):  # type: ignore[no-untyped-def]
        yield StrategyOutcome(status=RunStatus.FAILED, reason="planned failure")


class _CompletedStrategy:
    def __init__(self, config: dict[str, object]) -> None:
        pass

    async def execute(self, run: Run, rt: Runtime):  # type: ignore[no-untyped-def]
        yield StrategyOutcome(status=RunStatus.COMPLETED)


class _BudgetExceededStrategy:
    def __init__(self, config: dict[str, object]) -> None:
        pass

    async def execute(self, run: Run, rt: Runtime):  # type: ignore[no-untyped-def]
        if False:
            yield StrategyOutcome(status=RunStatus.COMPLETED)
        raise BudgetExceeded("run budget exhausted")


@pytest.mark.asyncio
async def test_strategy_failure_remains_failed_after_runtime_teardown(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    try:
        with patch.object(runtime, "_load_strategy", return_value=_FailedStrategy):
            run = await runtime.run(_task())

        persisted = await store.load_run(run.id)
        assert run.status == RunStatus.FAILED
        assert persisted.status == RunStatus.FAILED
        assert persisted.completed_at is not None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_stale_runtime_save_cannot_overwrite_cancellation(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    run = Run(
        task=_task(),
        status=RunStatus.RUNNING,
        workspace_path=tmp_path / "workspace",
    )
    try:
        await store.save_run(run)
        cancelled = await store.transition_run(run.id, RunStatus.ABORTED)
        assert cancelled.status == RunStatus.ABORTED

        run.status = RunStatus.COMPLETED
        await store.save_run(run)

        persisted = await store.load_run(run.id)
        assert persisted.status == RunStatus.ABORTED
        assert persisted.completed_at == cancelled.completed_at
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_run_timestamps_round_trip_unchanged(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    started = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    completed = datetime(2026, 1, 2, 4, 5, 6, tzinfo=UTC)
    run = Run(
        task=_task(),
        status=RunStatus.COMPLETED,
        workspace_path=tmp_path / "workspace",
        started_at=started,
        completed_at=completed,
    )
    try:
        await store.save_run(run)
        loaded = await store.load_run(run.id)
        assert loaded.started_at == started
        assert loaded.completed_at == completed
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_final_validator_rejection_prevents_completion(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    rejection = GateDecision(
        decision=GateAction.ABORT,
        reason="completion evidence rejected",
        validator_name="final-check",
    )
    try:
        with (
            patch.object(runtime, "_load_strategy", return_value=_CompletedStrategy),
            patch.object(
                runtime, "run_validators", new=AsyncMock(return_value=[rejection])
            ),
        ):
            run = await runtime.run(_task())

        assert run.status == RunStatus.ABORTED
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_final_validator_pause_remains_nonterminal(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    pause = GateDecision(
        decision=GateAction.PAUSE_FOR_HITL,
        reason="operator review required",
        validator_name="final-check",
    )
    try:
        with (
            patch.object(runtime, "_load_strategy", return_value=_CompletedStrategy),
            patch.object(
                runtime, "run_validators", new=AsyncMock(return_value=[pause])
            ),
        ):
            run = await runtime.run(_task())

        persisted = await store.load_run(run.id)
        assert run.status == RunStatus.PAUSED_HITL
        assert run.completed_at is None
        assert persisted.status == RunStatus.PAUSED_HITL
        assert persisted.completed_at is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_empty_final_validator_set_is_a_vacuous_pass(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    try:
        with patch.object(runtime, "_load_strategy", return_value=_CompletedStrategy):
            run = await runtime.run(_task())
        assert run.status == RunStatus.COMPLETED
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_budget_exception_persists_budget_terminal_state(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    try:
        with (
            patch.object(runtime, "_load_strategy", return_value=_BudgetExceededStrategy),
            pytest.raises(BudgetExceeded),
        ):
            await runtime.run(_task())

        runs = await store.list_runs()
        assert runs[0]["status"] == RunStatus.BUDGET_EXCEEDED.value
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_final_rejection_prevents_sequential_root_completion(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    task = _task().model_copy(
        update={
            "strategy": StrategyConfig(kind="sequential"),
            "agent": AgentConfig(
                type="mock", model="mock", extra={"steps": []}
            ),
        }
    )
    rejection = GateDecision(
        decision=GateAction.ABORT,
        reason="final evidence rejected",
        validator_name="final-check",
    )

    async def validator_policy(
        run: Run, session, *, when: str  # type: ignore[no-untyped-def]
    ) -> list[GateDecision]:
        return [rejection] if when == "final" else []

    try:
        with (
            patch.object(runtime, "run_validators", side_effect=validator_policy),
            patch(
                "horizonx.core.attempt_executor.build_agent",
                return_value=MockAgent(),
            ),
        ):
            run = await runtime.run(task)

        graph = await store.load_graph(run.id)
        assert run.status == RunStatus.ABORTED
        assert graph is not None
        assert graph.root.status != GoalStatus.DONE
    finally:
        await store.close()
