"""Shared terminal-outcome contract cases for built-in strategies."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from horizonx.agents.mock import MockAgent
from horizonx.core.goal_graph import GoalGraph
from horizonx.core.types import (
    AgentConfig,
    GateAction,
    GateDecision,
    HITLDecision,
    ResourceLimits,
    Run,
    RunStatus,
    Session,
    SessionRunResult,
    SessionStatus,
    StrategyConfig,
    StrategyOutcome,
    Task,
)
from horizonx.storage.sqlite import SqliteStore
from horizonx.strategies.decomposition import DecompositionFirst
from horizonx.strategies.monitor import MonitorRespond
from horizonx.strategies.pair import PairProgramming
from horizonx.strategies.ralph import RalphLoop
from horizonx.strategies.self_critique import SelfCritique
from horizonx.strategies.sequential import SequentialSubgoals
from horizonx.strategies.single import SingleSession
from horizonx.strategies.tree import TreeOfTrials


def _run(tmp_path: Path) -> Run:
    return Run(
        id="run-contract",
        task=Task(
            id="strategy-contract",
            name="Strategy contract",
            prompt="Exercise a strategy outcome",
            strategy=StrategyConfig(kind="single"),
            agent=AgentConfig(type="mock", model="mock"),
        ),
        workspace_path=tmp_path,
        status=RunStatus.RUNNING,
    )


def _runtime_double(store: object | None = None) -> MagicMock:
    rt = MagicMock()
    rt.store = store or MagicMock()
    rt.start_session = AsyncMock(
        return_value=Session(
            id="sess-contract", run_id="run-contract", sequence_index=0
        )
    )
    rt.end_session = AsyncMock()
    rt.record_step = AsyncMock()
    rt.run_validators = AsyncMock(return_value=[])
    rt.request_hitl = AsyncMock()
    return rt


async def _outcome(strategy, run: Run, rt: object) -> StrategyOutcome:  # type: ignore[no-untyped-def]
    items = [item async for item in strategy.execute(run, rt)]
    outcomes = [item for item in items if isinstance(item, StrategyOutcome)]
    assert len(outcomes) == 1
    assert items[-1] is outcomes[0]
    return outcomes[0]


@pytest.mark.asyncio
async def test_single_agent_error_is_a_failed_outcome(tmp_path: Path) -> None:
    rt = _runtime_double()
    agent = MockAgent(status=SessionStatus.ERRORED, error="provider failed")

    with patch("horizonx.strategies.single._build_agent", return_value=agent):
        outcome = await _outcome(SingleSession({}), _run(tmp_path), rt)

    assert outcome.status == RunStatus.FAILED
    assert outcome.reason == "agent_errored"
    rt.end_session.assert_awaited_once()
    rt.charge.assert_called_once()


@pytest.mark.asyncio
async def test_decomposition_agent_error_cannot_complete_goal(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    rt = _runtime_double(store)
    graph = GoalGraph.empty("Root", "root")
    agent = MockAgent(status=SessionStatus.ERRORED, error="provider failed")
    strategy = DecompositionFirst({})
    try:
        with (
            patch.object(strategy, "_decompose", new=AsyncMock(return_value=graph)),
            patch(
                "horizonx.strategies.decomposition._build_agent", return_value=agent
            ),
        ):
            outcome = await _outcome(strategy, _run(tmp_path), rt)

        persisted = await store.load_graph("run-contract")
        assert outcome.status == RunStatus.FAILED
        assert persisted is not None
        assert persisted.root.status.value == "failed"
        rt.end_session.assert_awaited_once_with(
            rt.start_session.return_value, SessionStatus.ERRORED
        )
        rt.charge.assert_called_once()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sequential_agent_error_cannot_complete_goal(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    rt = _runtime_double(store)
    graph = GoalGraph.empty("Root", "root")
    graph.save(tmp_path / "goals.json")
    await store.create_graph("run-contract", graph)
    agent = MockAgent(status=SessionStatus.ERRORED, error="provider failed")
    try:
        with patch(
            "horizonx.strategies.sequential._build_agent", return_value=agent
        ):
            outcome = await _outcome(SequentialSubgoals({}), _run(tmp_path), rt)

        persisted = await store.load_graph("run-contract")
        assert outcome.status == RunStatus.FAILED
        assert persisted is not None
        assert persisted.root.status.value == "failed"
        rt.end_session.assert_awaited_once_with(
            rt.start_session.return_value, SessionStatus.ERRORED
        )
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("strategy", "agent_target"),
    [
        (DecompositionFirst({}), "horizonx.strategies.decomposition._build_agent"),
        (SequentialSubgoals({}), "horizonx.strategies.sequential._build_agent"),
    ],
)
async def test_goal_strategy_validator_abort_is_an_aborted_outcome(
    tmp_path: Path, strategy: object, agent_target: str
) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    rt = _runtime_double(store)
    graph = GoalGraph.empty("Root", "root")
    graph.save(tmp_path / "goals.json")
    await store.create_graph("run-contract", graph)
    rt.run_validators = AsyncMock(
        return_value=[
            GateDecision(
                decision=GateAction.ABORT,
                reason="evidence rejected",
                validator_name="contract-check",
            )
        ]
    )
    try:
        with patch(agent_target, return_value=MockAgent()):
            outcome = await _outcome(strategy, _run(tmp_path), rt)

        assert outcome.status == RunStatus.ABORTED
        assert outcome.reason == "validator_aborted"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_decomposition_validator_pause_uses_operator_decision(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    rt = _runtime_double(store)
    graph = GoalGraph.empty("Root", "root")
    graph.save(tmp_path / "goals.json")
    await store.create_graph("run-contract", graph)
    calls = 0

    async def validator_policy(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            return [
                GateDecision(
                    decision=GateAction.PAUSE_FOR_HITL,
                    reason="operator review",
                    validator_name="contract-check",
                )
            ]
        return []

    rt.run_validators = AsyncMock(side_effect=validator_policy)
    rt.request_hitl = AsyncMock(return_value=HITLDecision(action="approve"))
    try:
        with patch(
            "horizonx.strategies.decomposition._build_agent", return_value=MockAgent()
        ):
            outcome = await _outcome(DecompositionFirst({}), _run(tmp_path), rt)

        assert outcome.status == RunStatus.COMPLETED
        rt.request_hitl.assert_awaited_once()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sequential_claim_loss_terminates_without_busy_loop(tmp_path: Path) -> None:
    graph = GoalGraph.empty("Root", "root")
    graph.save(tmp_path / "goals.json")
    store = MagicMock()
    store.load_graph = AsyncMock(return_value=graph)
    store.ensure_goal_projection = AsyncMock(return_value=False)
    store.claim_goal = AsyncMock(return_value=False)
    rt = _runtime_double(store)

    outcome = await asyncio.wait_for(
        _outcome(SequentialSubgoals({}), _run(tmp_path), rt), timeout=1
    )

    assert outcome.status == RunStatus.FAILED
    assert outcome.reason == "goal_claim_unavailable"
    rt.start_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_decomposition_claim_loss_terminates_without_busy_loop(
    tmp_path: Path,
) -> None:
    graph = GoalGraph.empty("Root", "root")
    graph.save(tmp_path / "goals.json")
    store = MagicMock()
    store.load_graph = AsyncMock(return_value=graph)
    store.ensure_goal_projection = AsyncMock(return_value=False)
    store.claim_goal = AsyncMock(return_value=False)
    rt = _runtime_double(store)

    outcome = await asyncio.wait_for(
        _outcome(DecompositionFirst({}), _run(tmp_path), rt), timeout=1
    )

    assert outcome.status == RunStatus.FAILED
    assert outcome.reason == "goal_claim_unavailable"
    rt.start_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_tree_below_threshold_is_not_completion(tmp_path: Path) -> None:
    strategy = TreeOfTrials({"width": 1, "max_depth": 1, "accept_threshold": 0.9})
    rt = _runtime_double()
    with (
        patch.object(strategy, "_run_branch", new=AsyncMock()),
        patch.object(strategy, "_score_branch", new=AsyncMock(return_value=0.4)),
    ):
        outcome = await _outcome(strategy, _run(tmp_path), rt)

    assert outcome.status == RunStatus.FAILED
    assert outcome.reason == "acceptance_threshold_not_met"


@pytest.mark.asyncio
async def test_tree_cannot_select_a_failed_branch(tmp_path: Path) -> None:
    strategy = TreeOfTrials({"width": 1, "max_depth": 1, "accept_threshold": 0})
    rt = _runtime_double()
    with patch.object(
        strategy, "_run_branch", new=AsyncMock(side_effect=RuntimeError("branch failed"))
    ):
        outcome = await _outcome(strategy, _run(tmp_path), rt)

    assert outcome.status == RunStatus.FAILED
    assert outcome.reason == "all_branches_failed"


@pytest.mark.asyncio
async def test_pair_navigator_failure_is_not_completion(tmp_path: Path) -> None:
    strategy = PairProgramming({"max_rounds": 1})
    rt = _runtime_double()
    agent = MagicMock()
    agent.run_session = AsyncMock(
        side_effect=[
            SessionRunResult(status=SessionStatus.COMPLETED),
            SessionRunResult(status=SessionStatus.ERRORED, error="review failed"),
        ]
    )
    with patch("horizonx.strategies.pair._build_agent", return_value=agent):
        outcome = await _outcome(strategy, _run(tmp_path), rt)

    assert outcome.status == RunStatus.FAILED
    assert outcome.reason == "navigator_failed"


@pytest.mark.asyncio
async def test_self_critique_reviewer_failure_is_not_completion(
    tmp_path: Path,
) -> None:
    strategy = SelfCritique(
        {"max_rounds": 1, "critic_type": "agent", "accept_threshold": 0.9}
    )
    rt = _runtime_double()
    agent = MagicMock()
    agent.run_session = AsyncMock(
        side_effect=[
            SessionRunResult(status=SessionStatus.COMPLETED),
            SessionRunResult(status=SessionStatus.ERRORED, error="review failed"),
        ]
    )
    with patch("horizonx.strategies.self_critique._build_agent", return_value=agent):
        outcome = await _outcome(strategy, _run(tmp_path), rt)

    assert outcome.status == RunStatus.FAILED


@pytest.mark.asyncio
async def test_monitor_time_limit_is_a_timed_out_outcome(tmp_path: Path) -> None:
    strategy = MonitorRespond({"poll_interval_seconds": 0})
    run = _run(tmp_path)
    run.task.resources = ResourceLimits(max_total_hours=0)

    outcome = await _outcome(strategy, run, _runtime_double())

    assert outcome.status == RunStatus.TIMED_OUT


@pytest.mark.asyncio
async def test_monitor_responder_failure_is_a_failed_outcome(tmp_path: Path) -> None:
    strategy = MonitorRespond({"poll_interval_seconds": 0, "max_triggers": 1})
    agent = MockAgent(status=SessionStatus.ERRORED, error="response failed")
    with (
        patch.object(strategy, "_check_trigger", new=AsyncMock(return_value=True)),
        patch("horizonx.strategies.monitor._build_agent", return_value=agent),
    ):
        outcome = await _outcome(strategy, _run(tmp_path), _runtime_double())

    assert outcome.status == RunStatus.FAILED
    assert outcome.reason == "responder_errored"


@pytest.mark.asyncio
async def test_ralph_missing_baseline_metric_is_not_completion(tmp_path: Path) -> None:
    strategy = RalphLoop({"total_minutes": 0})
    with (
        patch.object(strategy, "_git_init"),
        patch.object(strategy, "_measure", new=AsyncMock(return_value=None)),
    ):
        outcome = await _outcome(strategy, _run(tmp_path), _runtime_double())

    assert outcome.status == RunStatus.FAILED
    assert outcome.reason == "baseline_metric_unavailable"


@pytest.mark.asyncio
async def test_ralph_all_iteration_timeouts_are_not_completion(tmp_path: Path) -> None:
    strategy = RalphLoop({"total_minutes": 1, "fixed_minutes_per_iter": 1})

    async def timeout_once(awaitable, *, timeout):  # type: ignore[no-untyped-def]
        awaitable.close()
        strategy.total_minutes = 0
        raise TimeoutError

    with (
        patch.object(strategy, "_git_init"),
        patch.object(strategy, "_measure", new=AsyncMock(return_value=1.0)),
        patch("horizonx.strategies.ralph.asyncio.wait_for", side_effect=timeout_once),
    ):
        outcome = await _outcome(strategy, _run(tmp_path), _runtime_double())

    assert outcome.status == RunStatus.TIMED_OUT
    assert outcome.reason == "all_iterations_timed_out"
