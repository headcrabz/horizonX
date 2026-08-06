"""End-to-end invariants for run terminal states."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from horizonx.agents.mock import MockAgent
from horizonx.core.governor import BudgetExceeded
from horizonx.core.operator_commands import OperatorCommand, OperatorCommandKind
from horizonx.core.runtime import Runtime
from horizonx.core.types import (
    AgentConfig,
    GateAction,
    GateDecision,
    GoalStatus,
    Run,
    RunStatus,
    SpinDetectionConfig,
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


class _SwitchingStrategy:
    closed = False

    def __init__(self, config: dict[str, object]) -> None:
        pass

    async def execute(self, run: Run, rt: Runtime):  # type: ignore[no-untyped-def]
        from horizonx.core.attempt_executor import AttemptExecutor

        try:
            await AttemptExecutor(rt).execute(run, prompt="trigger the switch")
            yield StrategyOutcome(status=RunStatus.FAILED, reason="source continued")
        finally:
            type(self).closed = True


class _SwitchTargetStrategy:
    executions = 0

    def __init__(self, config: dict[str, object]) -> None:
        pass

    async def execute(self, run: Run, rt: Runtime):  # type: ignore[no-untyped-def]
        type(self).executions += 1
        yield StrategyOutcome(status=RunStatus.COMPLETED, reason="target completed")


class _TerminalThenSwitchStrategy:
    def __init__(self, config: dict[str, object]) -> None:
        pass

    async def execute(self, run: Run, rt: Runtime):  # type: ignore[no-untyped-def]
        yield StrategyOutcome(status=RunStatus.COMPLETED)
        assert await rt.request_strategy_switch(run, target="sequential") is False


class _ConcurrentSwitchStrategy:
    entrants = 0
    ready = None

    def __init__(self, config: dict[str, object]) -> None:
        pass

    async def execute(self, run: Run, rt: Runtime):  # type: ignore[no-untyped-def]
        import asyncio

        from horizonx.core.strategy_switch import StrategySwitchRequested

        type(self).entrants += 1
        if type(self).entrants == 2:
            assert type(self).ready is not None
            type(self).ready.set()
        assert type(self).ready is not None
        await asyncio.wait_for(type(self).ready.wait(), timeout=2)
        assert await rt.request_strategy_switch(run, target="sequential")
        raise StrategySwitchRequested(run.id, "sequential")
        yield StrategyOutcome(status=RunStatus.FAILED)  # pragma: no cover


class _AttemptBackedStrategy:
    executions = 0

    def __init__(self, config: dict[str, object]) -> None:
        pass

    async def execute(self, run: Run, rt: Runtime):  # type: ignore[no-untyped-def]
        from horizonx.core.attempt_executor import AttemptExecutor

        type(self).executions += 1
        attempt = await AttemptExecutor(rt).execute(run, prompt="work")
        yield StrategyOutcome(
            status=RunStatus.COMPLETED if attempt.succeeded else RunStatus.FAILED
        )


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
async def test_terminal_run_cannot_be_resumed(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    task = _task()
    try:
        completed = await runtime.run(task)
        attempts_before = await store.list_attempts(completed.id)

        with pytest.raises(ValueError, match="cannot resume terminal run"):
            await runtime.run(task, resume_from=completed.id)

        attempts_after = await store.list_attempts(completed.id)
        assert attempts_after == attempts_before
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_resume_rejects_a_different_task_snapshot(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    persisted = Run(
        task=_task(),
        status=RunStatus.RUNNING,
        workspace_path=tmp_path / "workspace",
    )
    await store.save_run(persisted)
    changed = _task().model_copy(update={"prompt": "Different work"})
    try:
        with pytest.raises(ValueError, match="task snapshot does not match"):
            await runtime.run(changed, resume_from=persisted.id)
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
@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("approve", RunStatus.COMPLETED),
        ("abort", RunStatus.ABORTED),
        ("modify", RunStatus.FAILED),
        ("re_decompose", RunStatus.FAILED),
    ],
)
async def test_final_validator_pause_uses_durable_decision_flow(
    tmp_path: Path, action: str, expected: RunStatus,
) -> None:
    import asyncio

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
            execution = asyncio.create_task(runtime.run(_task()))
            for _ in range(100):
                runs = await store.list_nonterminal_runs()
                requests = (
                    await store.list_hitl_events(runs[0].id) if runs else []
                )
                if requests:
                    break
                await asyncio.sleep(0.01)
            assert requests
            request = requests[-1]
            await store.submit_active_hitl_decision(
                OperatorCommand(
                    run_id=runs[0].id,
                    kind=OperatorCommandKind.DECISION,
                    actor="reviewer",
                    reason="final review",
                    instruction="apply final guidance",
                    payload={"request_id": request["id"], "action": action},
                    idempotency_key=f"final-{action}",
                )
            )
            run = await asyncio.wait_for(execution, timeout=1)

        assert run.status == expected
        assert (await store.load_run(run.id)).status == expected
        [requested] = await store.list_events(run.id, event_type="hitl.requested")
        assert requested.id == f"hitl.requested:{request['id']}"
        [resolved] = await store.list_events(run.id, event_type="hitl.resolved")
        assert resolved.payload["action"] == action
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
async def test_spin_switch_closes_source_and_hands_off_once_after_durable_attempt(
    tmp_path: Path,
) -> None:
    from horizonx.core.types import SpinReport
    from tests.test_attempt_executor import _RepeatingAgent

    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    task = _task().model_copy(
        update={
            "spin_detection": SpinDetectionConfig(
                on_spin="switch_strategy", switch_strategy_to="sequential"
            )
        }
    )
    runtime.check_spin = AsyncMock(  # type: ignore[method-assign]
        return_value=SpinReport(
            detected=True,
            layer="score_plateau",
            action="switch_strategy",
        )
    )
    _SwitchingStrategy.closed = False
    _SwitchTargetStrategy.executions = 0

    def load_strategy(kind: str):  # type: ignore[no-untyped-def]
        return {
            "single": _SwitchingStrategy,
            "sequential": _SwitchTargetStrategy,
        }[kind]

    try:
        with (
            patch.object(runtime, "_load_strategy", side_effect=load_strategy),
            patch(
                "horizonx.core.attempt_executor.build_agent",
                return_value=_RepeatingAgent(),
            ),
        ):
            run = await runtime.run(task)

        assert run.status == RunStatus.COMPLETED
        assert _SwitchingStrategy.closed is True
        assert _SwitchTargetStrategy.executions == 1
        sessions = await store.list_sessions(run.id)
        attempts = await store.list_attempts(run.id)
        switch_events = await store.list_events(run.id, event_type="strategy.switched")
        terminal_events = [
            event
            for event in await store.list_events(run.id)
            if event.type in {"run.completed", "run.failed"}
        ]
        assert sessions[0].status.value == "spin"
        assert attempts[0].status.value in {"failed", "interrupted"}
        assert len(switch_events) == 1
        assert switch_events[0].payload["from"] == "single"
        assert switch_events[0].payload["to"] == "sequential"
        assert [event.type for event in terminal_events] == ["run.completed"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_strategy_switch_rejects_same_target_and_second_request(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    run = Run(task=_task(), status=RunStatus.RUNNING, workspace_path=tmp_path)

    await runtime._begin_strategy_execution(run, "single")
    try:
        assert await runtime.request_strategy_switch(run, target="single") is False
        assert await runtime.request_strategy_switch(run, target="unknown") is False
        assert await runtime.request_strategy_switch(run, target="sequential") is True
        runtime._mark_strategy_switched(run, "sequential")
        assert await runtime.request_strategy_switch(run, target="tree") is False
    finally:
        runtime._end_strategy_execution(run.id)
        await store.close()


@pytest.mark.asyncio
async def test_durable_switch_event_prevents_a_second_switch_after_resume(
    tmp_path: Path,
) -> None:
    from horizonx.core.event_bus import Event

    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    run = Run(task=_task(), status=RunStatus.RUNNING, workspace_path=tmp_path)
    await store.save_run(run)
    await store.append_event(
        Event(
            type="strategy.switched",
            run_id=run.id,
            payload={"from": "single", "to": "sequential"},
        )
    )

    current = await runtime._begin_strategy_execution(run, "single")
    try:
        assert current == "sequential"
        assert await runtime.request_strategy_switch(run, target="tree") is False
    finally:
        runtime._end_strategy_execution(run.id)
        await store.close()


@pytest.mark.asyncio
async def test_two_runtimes_atomically_publish_only_one_strategy_switch(
    tmp_path: Path,
) -> None:
    import asyncio

    from horizonx.core.event_bus import Event

    path = tmp_path / "horizonx.db"
    first_store = SqliteStore(path)
    second_store = SqliteStore(path)
    first = Runtime(first_store, workspace_root=tmp_path / "first")
    second = Runtime(second_store, workspace_root=tmp_path / "second")
    run = Run(task=_task(), status=RunStatus.RUNNING, workspace_path=tmp_path)
    await first_store.save_run(run)
    try:
        published = await asyncio.gather(
            first._publish_strategy_switch(
                Event(type="strategy.switched", run_id=run.id, payload={"from": "single", "to": "tree"})
            ),
            second._publish_strategy_switch(
                Event(type="strategy.switched", run_id=run.id, payload={"from": "single", "to": "sequential"})
            ),
        )
        events = await first_store.list_events(run.id, event_type="strategy.switched")
        assert len(events) == 1
        assert published[0][0].id == published[1][0].id == events[0].id
        assert sorted(result[1] for result in published) == [False, True]
    finally:
        await first_store.close()
        await second_store.close()


@pytest.mark.asyncio
async def test_resume_loads_durable_target_without_resolving_removed_source(
    tmp_path: Path,
) -> None:
    from horizonx.core.event_bus import Event

    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    task = _task()
    persisted = Run(
        task=task,
        status=RunStatus.RUNNING,
        workspace_path=tmp_path / "workspace",
    )
    await store.save_run(persisted)
    await store.append_event(
        Event(
            type="strategy.switched",
            run_id=persisted.id,
            payload={"from": "single", "to": "sequential"},
        )
    )

    def load_strategy(kind: str):  # type: ignore[no-untyped-def]
        if kind == "single":
            raise ValueError("removed source plugin")
        assert kind == "sequential"
        return _SwitchTargetStrategy

    try:
        with patch.object(runtime, "_load_strategy", side_effect=load_strategy):
            resumed = await runtime.run(task, resume_from=persisted.id)
        assert resumed.status == RunStatus.COMPLETED
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_two_concurrent_runtimes_execute_switch_target_and_terminalize_once(
    tmp_path: Path,
) -> None:
    import asyncio

    path = tmp_path / "horizonx.db"
    stores = [SqliteStore(path), SqliteStore(path)]
    runtimes = [
        Runtime(stores[0], workspace_root=tmp_path / "first"),
        Runtime(stores[1], workspace_root=tmp_path / "second"),
    ]
    task = _task()
    persisted = Run(
        task=task,
        status=RunStatus.RUNNING,
        workspace_path=tmp_path / "workspace",
    )
    await stores[0].save_run(persisted)
    _ConcurrentSwitchStrategy.entrants = 0
    _ConcurrentSwitchStrategy.ready = asyncio.Event()
    _SwitchTargetStrategy.executions = 0

    def load_strategy(kind: str):  # type: ignore[no-untyped-def]
        return {
            "single": _ConcurrentSwitchStrategy,
            "sequential": _SwitchTargetStrategy,
        }[kind]

    try:
        with (
            patch.object(runtimes[0], "_load_strategy", side_effect=load_strategy),
            patch.object(runtimes[1], "_load_strategy", side_effect=load_strategy),
        ):
            await asyncio.gather(
                *(runtime.run(task, resume_from=persisted.id) for runtime in runtimes)
            )
        assert _SwitchTargetStrategy.executions == 1
        terminal = [
            event
            for event in await stores[0].list_events(persisted.id)
            if event.type in {"run.completed", "run.failed"}
        ]
        assert len(terminal) == 1
    finally:
        await stores[0].close()
        await stores[1].close()


@pytest.mark.asyncio
async def test_switch_after_source_terminal_outcome_is_rejected(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    try:
        with patch.object(
            runtime, "_load_strategy", return_value=_TerminalThenSwitchStrategy
        ):
            run = await runtime.run(_task())

        assert run.status == RunStatus.COMPLETED
        assert await store.list_events(run.id, event_type="strategy.switched") == []
        terminal = [
            event.type
            for event in await store.list_events(run.id)
            if event.type in {"run.completed", "run.failed"}
        ]
        assert terminal == ["run.completed"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_runtime_retries_same_strategy_once_after_hard_spin(tmp_path: Path) -> None:
    from horizonx.core.types import SpinReport
    from tests.test_attempt_executor import _RepeatingAgent

    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    task = _task()
    reports = [
        SpinReport(detected=True, layer="loop", action="terminate_session_and_retry"),
        None,
    ]
    runtime.check_spin = AsyncMock(side_effect=reports)  # type: ignore[method-assign]
    _AttemptBackedStrategy.executions = 0
    try:
        with (
            patch.object(runtime, "_load_strategy", return_value=_AttemptBackedStrategy),
            patch("horizonx.core.attempt_executor.build_agent", return_value=_RepeatingAgent()),
        ):
            run = await runtime.run(task)
        assert run.status == RunStatus.COMPLETED
        assert _AttemptBackedStrategy.executions == 2
        assert len(await store.list_attempts(run.id)) == 2
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_runtime_hard_spin_uses_authenticated_durable_hitl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    pytest.importorskip("httpx")
    from httpx import ASGITransport, AsyncClient

    from horizonx.core.event_bus import InMemoryBus
    from horizonx.core.types import SpinReport
    from horizonx.dashboard.app import create_app
    from tests.test_attempt_executor import _RepeatingAgent

    store = SqliteStore(tmp_path / "horizonx.db")
    bus = InMemoryBus()
    runtime = Runtime(store=store, bus=bus, workspace_root=tmp_path / "workspaces")
    app = create_app(tmp_path / "horizonx.db", tmp_path / "workspaces")
    app.state.store = store
    app.state.bus = bus
    app.state.runtime = runtime
    monkeypatch.setenv("HORIZONX_OPERATOR_TOKEN", "spin-review-token")
    runtime.check_spin = AsyncMock(  # type: ignore[method-assign]
        return_value=SpinReport(detected=True, layer="loop", action="terminate_and_hitl")
    )
    try:
        with (
            patch.object(runtime, "_load_strategy", return_value=_AttemptBackedStrategy),
            patch("horizonx.core.attempt_executor.build_agent", return_value=_RepeatingAgent()),
        ):
            execution = asyncio.create_task(runtime.run(_task()))
            for _ in range(100):
                runs = await store.list_nonterminal_runs()
                requests = await store.list_hitl_events(runs[0].id) if runs else []
                if requests:
                    break
                await asyncio.sleep(0.01)
            assert requests
            run_id = requests[-1]["run_id"]
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    f"/api/runs/{run_id}/hitl",
                    headers={"authorization": "Bearer spin-review-token"},
                    json={"action": "approve", "request_id": requests[-1]["id"]},
                )
            assert response.status_code == 200
            run = await asyncio.wait_for(execution, timeout=1)
        assert run.status in {RunStatus.FAILED, RunStatus.TIMED_OUT}
        assert run.status != RunStatus.PAUSED_HITL
        assert len(await store.list_events(run.id, event_type="hitl.requested")) == 1
        assert (await store.list_hitl_events(run.id))[0]["resolved_at"] is not None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_hard_spin_cancel_winner_starts_no_more_work(tmp_path: Path) -> None:
    import asyncio

    from horizonx.core.types import SpinReport
    from tests.test_attempt_executor import _RepeatingAgent

    path = tmp_path / "horizonx.db"
    store = SqliteStore(path)
    cancel_store = SqliteStore(path)
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    runtime.check_spin = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            SpinReport(detected=True, layer="loop", action="terminate_and_hitl"),
            None,
        ]
    )
    apply_started = asyncio.Event()
    allow_apply = asyncio.Event()
    original_apply = store.apply_hitl_decision
    original_register = runtime.register_cancel_token
    registered_tokens: list[object] = []

    async def apply_after_cancel(*args: object, **kwargs: object):
        apply_started.set()
        await allow_apply.wait()
        return await original_apply(*args, **kwargs)

    def track_cancel_token(run_id: str, token: object) -> None:
        registered_tokens.append(token)
        original_register(run_id, token)

    store.apply_hitl_decision = apply_after_cancel  # type: ignore[method-assign]
    runtime.register_cancel_token = track_cancel_token  # type: ignore[method-assign]
    _AttemptBackedStrategy.executions = 0
    execution: asyncio.Task[Run] | None = None
    try:
        with (
            patch.object(runtime, "_load_strategy", return_value=_AttemptBackedStrategy),
            patch(
                "horizonx.core.attempt_executor.build_agent",
                return_value=_RepeatingAgent(),
            ),
        ):
            execution = asyncio.create_task(runtime.run(_task()))
            for _ in range(100):
                runs = await store.list_nonterminal_runs()
                requests = await store.list_hitl_events(runs[0].id) if runs else []
                if requests:
                    break
                await asyncio.sleep(0.01)
            assert requests
            run_id = requests[-1]["run_id"]
            await store.submit_active_hitl_decision(
                OperatorCommand(
                    run_id=run_id,
                    kind=OperatorCommandKind.DECISION,
                    actor="human-operator",
                    instruction="continue after spin",
                    payload={
                        "request_id": requests[-1]["id"],
                        "action": "modify",
                    },
                    idempotency_key="spin-human-modify",
                )
            )
            await asyncio.wait_for(apply_started.wait(), timeout=1)
            await cancel_store.submit_cancel_command(
                OperatorCommand(
                    run_id=run_id,
                    kind=OperatorCommandKind.CANCEL,
                    actor="cancel-operator",
                    reason="stop after hard spin",
                    idempotency_key="spin-terminal-cancel",
                )
            )
            allow_apply.set()
            run = await asyncio.wait_for(execution, timeout=1)

        assert run.status == RunStatus.ABORTED
        assert (await cancel_store.load_run(run.id)).status == RunStatus.ABORTED
        assert _AttemptBackedStrategy.executions == 1
        assert len(await store.list_attempts(run.id)) == 1
        assert len(registered_tokens) == 1
        assert await store.list_events(run.id, event_type="goals.re_decomposed") == []
    finally:
        allow_apply.set()
        if execution is not None and not execution.done():
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
        await store.close()
        await cancel_store.close()


@pytest.mark.asyncio
async def test_hard_spin_hitl_survives_runtime_crash_then_cancel(
    tmp_path: Path,
) -> None:
    import asyncio

    from horizonx.core.operator_commands import OperatorCommand, OperatorCommandKind
    from horizonx.core.types import SpinReport
    from tests.test_attempt_executor import _RepeatingAgent

    path = tmp_path / "horizonx.db"
    store = SqliteStore(path)
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    runtime.check_spin = AsyncMock(  # type: ignore[method-assign]
        return_value=SpinReport(detected=True, layer="loop", action="terminate_and_hitl")
    )
    with (
        patch.object(runtime, "_load_strategy", return_value=_AttemptBackedStrategy),
        patch("horizonx.core.attempt_executor.build_agent", return_value=_RepeatingAgent()),
    ):
        execution = asyncio.create_task(runtime.run(_task()))
        for _ in range(100):
            runs = await store.list_nonterminal_runs()
            requests = await store.list_hitl_events(runs[0].id) if runs else []
            if requests:
                break
            await asyncio.sleep(0.01)
        assert requests
        run_id = requests[-1]["run_id"]
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution
    assert (await store.load_run(run_id)).status == RunStatus.PAUSED_HITL
    assert len(await store.list_events(run_id, event_type="hitl.requested")) == 1
    await store.close()

    restarted = SqliteStore(path)
    try:
        await restarted.submit_cancel_command(
            OperatorCommand(
                run_id=run_id, kind=OperatorCommandKind.CANCEL,
                actor="restart-operator", reason="cancel after crash",
                idempotency_key="spin-crash-cancel",
            )
        )
        assert (await restarted.load_run(run_id)).status == RunStatus.ABORTED
        [request] = await restarted.list_hitl_events(run_id)
        assert request["decision"] == "abort"
        assert request["resolved_at"] is not None
    finally:
        await restarted.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["disabled", "unconfigured"])
async def test_hard_spin_policy_skip_never_status_only_pauses(
    tmp_path: Path, mode: str,
) -> None:
    from horizonx.core.types import SpinReport
    from tests.test_attempt_executor import _RepeatingAgent

    store = SqliteStore(tmp_path / f"{mode}.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / mode)
    runtime.check_spin = AsyncMock(  # type: ignore[method-assign]
        return_value=SpinReport(detected=True, layer="loop", action="terminate_and_hitl")
    )
    task = _task()
    if mode == "disabled":
        task.hitl.enabled = False
    else:
        task.hitl.triggers = ["validator_paused"]
    try:
        with (
            patch.object(runtime, "_load_strategy", return_value=_AttemptBackedStrategy),
            patch("horizonx.core.attempt_executor.build_agent", return_value=_RepeatingAgent()),
        ):
            run = await runtime.run(task)
        assert run.status in {RunStatus.FAILED, RunStatus.TIMED_OUT}
        assert run.status != RunStatus.PAUSED_HITL
        assert await store.list_hitl_events(run.id) == []
        assert await store.list_events(run.id, event_type="hitl.requested") == []
    finally:
        await store.close()


def test_runtime_has_no_status_only_hitl_helpers(tmp_path: Path) -> None:
    runtime = Runtime(object(), workspace_root=tmp_path)
    assert not hasattr(runtime, "_pause_run_for_spin")
    assert not hasattr(runtime, "_pause_run")


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
