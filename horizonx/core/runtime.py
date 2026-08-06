"""Runtime — the central orchestrator.

See docs/LONG_HORIZON_AGENT.md §11.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from horizonx.core.event_bus import DurableEventBus, Event, EventBus, InMemoryBus
from horizonx.core.governor import BudgetExceeded, ResourceGovernor
from horizonx.core.recorder import TrajectoryRecorder
from horizonx.core.spin_detector import CrossSessionSpinLayer, SpinDetector
from horizonx.core.strategy_switch import SpinControlRequested, StrategySwitchRequested
from horizonx.core.summarizer import Summarizer
from horizonx.core.types import (
    TERMINAL_RUN_STATUSES,
    GateAction,
    GoalNode,
    HITLDecision,
    Run,
    RunStatus,
    Session,
    SessionStatus,
    Step,
    StepType,
    StrategyOutcome,
    Task,
    ValidatorConfig,
    new_run_id,
    new_session_id,
)
from horizonx.environments.base import PreparedWorkspace, SetupCommandError, WorkspaceError
from horizonx.environments.git import GitWorktreeBackend

_BUILTIN_STRATEGIES = {
    "decomposition": "horizonx.strategies.decomposition:DecompositionFirst",
    "monitor": "horizonx.strategies.monitor:MonitorRespond",
    "pair": "horizonx.strategies.pair:PairProgramming",
    "ralph": "horizonx.strategies.ralph:RalphLoop",
    "self_critique": "horizonx.strategies.self_critique:SelfCritique",
    "sequential": "horizonx.strategies.sequential:SequentialSubgoals",
    "single": "horizonx.strategies.single:SingleSession",
    "tree": "horizonx.strategies.tree:TreeOfTrials",
}


@dataclass
class _StrategyExecution:
    current: str
    requested: str | None = None
    switched: bool = False
    terminal_outcome_seen: bool = False
    retry_used: bool = False


class Runtime:
    """Top-level orchestrator. One Runtime serves N concurrent Runs.

    Strategy-agnostic: provides primitives. Strategies decide when to call them.
    """

    def __init__(
        self,
        store: Any,  # Storage protocol; avoid circular import
        bus: EventBus | None = None,
        workspace_root: Path = Path("./horizonx-workspaces"),
    ) -> None:
        self.store = store
        downstream = bus or InMemoryBus()
        self.bus: EventBus = (
            DurableEventBus(store, downstream)
            if hasattr(store, "append_event")
            else downstream
        )
        self.workspace_root = workspace_root
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.recorder = TrajectoryRecorder(store=store, bus=self.bus)
        self._governors: dict[str, ResourceGovernor] = {}
        self._workspace_run_counts: dict[str, int] = {}
        self._workspace_run_lock = asyncio.Lock()
        self._last_sessions: dict[str, Session] = {}
        self._prepared_workspaces: dict[str, PreparedWorkspace] = {}
        self._recovery_contexts: dict[str, dict[str, str | None]] = {}
        self._strategy_executions: dict[str, _StrategyExecution] = {}
        self._background_runs: dict[str, asyncio.Task[Any]] = {}
        self._command_notifications: dict[str, asyncio.Event] = {}
        self._active_cancel_tokens: dict[str, dict[int, Any]] = {}

    async def __aenter__(self) -> Runtime:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.shutdown()

    def start_background_run(
        self, run_id: str, coroutine: Any, *, name: str
    ) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine, name=name)
        self._background_runs[run_id] = task
        task.add_done_callback(lambda completed: self._background_runs.pop(run_id, None))
        return task

    def register_cancel_token(self, run_id: str, token: Any) -> None:
        self._active_cancel_tokens.setdefault(run_id, {})[id(token)] = token

    def unregister_cancel_token(self, run_id: str, token: Any) -> None:
        tokens = self._active_cancel_tokens.get(run_id)
        if tokens is None:
            return
        tokens.pop(id(token), None)
        if not tokens:
            self._active_cancel_tokens.pop(run_id, None)

    def notify_operator_command(self, run_id: str, reason: str | None = None) -> None:
        event = self._command_notifications.get(run_id)
        if event is not None:
            event.set()
        if reason is not None:
            for token in tuple(self._active_cancel_tokens.get(run_id, {}).values()):
                token.cancel(reason)

    async def shutdown(self, *, close_store: bool = True) -> None:
        tasks = list(self._background_runs.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_runs.clear()
        if close_store and hasattr(self.store, "close"):
            await self.store.close()

    # ---------------------------------------------------------------
    # Top-level entry
    # ---------------------------------------------------------------

    async def run(
        self,
        task: Task,
        *,
        resume_from: str | None = None,
        resume_provider_session_id: str | None = None,
        recovery_lineage_id: str | None = None,
        retry_cause: str | None = None,
    ) -> Run:
        async with self._workspace_run_slot(task):
            return await self._run_with_resources(
                task,
                resume_from=resume_from,
                resume_provider_session_id=resume_provider_session_id,
                recovery_lineage_id=recovery_lineage_id,
                retry_cause=retry_cause,
            )

    async def _run_with_resources(
        self,
        task: Task,
        *,
        resume_from: str | None = None,
        resume_provider_session_id: str | None = None,
        recovery_lineage_id: str | None = None,
        retry_cause: str | None = None,
    ) -> Run:
        # Pre-flight workspace daily budget check
        if task.workspace and task.workspace.daily_budget_usd is not None:
            from horizonx.core.usage import UsageStore
            spent = await UsageStore(self.store).daily_usd(task.workspace.workspace_id)
            if spent is not None and spent >= task.workspace.daily_budget_usd:
                raise BudgetExceeded(
                    f"workspace {task.workspace.workspace_id!r} daily budget "
                    f"${task.workspace.daily_budget_usd:.2f} already spent (${spent:.2f} today)"
                )
        run = await self._load_or_create(task, resume_from)
        current_kind = await self._begin_strategy_execution(run, task.strategy.kind)
        # Resolve the durable current strategy before touching a repository.
        strategy_cls = self._load_strategy(current_kind)
        await self.store.save_run(run)
        try:
            workspace_metadata = run.workspace_path / ".horizonx" / "workspace.json"
            await self.prepare_workspace(
                run,
                resume=resume_from is not None and workspace_metadata.is_file(),
            )
        except WorkspaceError as exc:
            self._end_strategy_execution(run.id)
            await self._finish_run(
                run,
                StrategyOutcome(
                    status=RunStatus.FAILED,
                    reason=(
                        "workspace_setup_failed"
                        if isinstance(exc, SetupCommandError)
                        else "workspace_preparation_failed"
                    ),
                    details={"error": str(exc)},
                ),
            )
            raise
        await self.bus.publish(Event(type="run.started", run_id=run.id))

        # GE-01: decomposition quality pre-check (after goals.json exists)
        goals_path = run.workspace_path / "goals.json"
        if goals_path.exists():
            from horizonx.core.decomposition_checker import DecompositionChecker
            report = DecompositionChecker().check_file(goals_path, task)
            run.decomposition_report = report.to_dict()
            if report.has_errors:
                await self._finish_run(
                    run,
                    StrategyOutcome(
                        status=RunStatus.FAILED,
                        reason="decomposition_errors",
                        details={"report": report.to_dict()},
                    ),
                )
                self._end_strategy_execution(run.id)
                return run

        if recovery_lineage_id or resume_provider_session_id or retry_cause:
            self._recovery_contexts[run.id] = {
                "provider_session_id": resume_provider_session_id,
                "lineage_id": recovery_lineage_id,
                "retry_cause": retry_cause,
            }

        async with self._governor(run):
            try:
                current_cls = strategy_cls
                outcome: StrategyOutcome | None = None
                while True:
                    strategy = current_cls(task.strategy.config)
                    outcome = None
                    iterator = strategy.execute(run, self).__aiter__()
                    try:
                        async for item in iterator:
                            if isinstance(item, StrategyOutcome):
                                if outcome is not None:
                                    raise RuntimeError(
                                        "strategy yielded more than one terminal outcome"
                                    )
                                outcome = item
                                self._strategy_executions[
                                    run.id
                                ].terminal_outcome_seen = True
                                continue
                            if outcome is not None:
                                raise RuntimeError(
                                    "strategy yielded an event after its terminal outcome"
                                )
                            await self.bus.publish(item)
                            # Re-run decomposition check after graph is first written
                            if goals_path.exists() and (
                                not hasattr(run, "decomposition_report")
                                or not run.decomposition_report
                            ):
                                from horizonx.core.decomposition_checker import (
                                    DecompositionChecker,
                                )

                                rpt = DecompositionChecker().check_file(goals_path, task)
                                run.decomposition_report = rpt.to_dict()
                    except StrategySwitchRequested as request:
                        await iterator.aclose()
                        if outcome is not None:
                            raise RuntimeError(
                                "strategy requested a switch after terminal outcome"
                            ) from request
                        if request.run_id != run.id:
                            raise RuntimeError(
                                "strategy switch belongs to another run"
                            ) from request
                        target = self.pending_strategy_switch(run.id)
                        if target != request.target:
                            raise RuntimeError(
                                "strategy switch request was not pending"
                            ) from request
                        next_cls = self._load_strategy(target)
                        switch_event = Event(
                            type="strategy.switched",
                            run_id=run.id,
                            payload={
                                "from": current_kind,
                                "to": target,
                                "reason": "spin_detected",
                            },
                        )
                        persisted_switch, owns_switch = await self._publish_strategy_switch(
                            switch_event
                        )
                        if not owns_switch:
                            return run
                        durable_target = persisted_switch.payload.get("to")
                        if not isinstance(durable_target, str) or not durable_target:
                            raise RuntimeError(
                                "durable strategy switch has no target"
                            ) from request
                        if durable_target != target:
                            next_cls = self._load_strategy(durable_target)
                            context = self._strategy_executions[run.id]
                            context.requested = durable_target
                        self._mark_strategy_switched(run, durable_target)
                        current_kind = durable_target
                        current_cls = next_cls
                        continue
                    except SpinControlRequested as request:
                        await iterator.aclose()
                        if request.run_id != run.id or outcome is not None:
                            raise RuntimeError("invalid post-attempt spin control") from request
                        if request.action == "terminate_session_and_retry":
                            self._strategy_executions[
                                run.id
                            ].terminal_outcome_seen = False
                            continue
                        if request.action == "terminate_and_hitl":
                            decision = await self.request_hitl(
                                run,
                                reason="spin_detected",
                                context={
                                    "control": request.action,
                                    "strategy": current_kind,
                                },
                            )
                            if decision.action == "abort":
                                await self._finish_run(
                                    run,
                                    StrategyOutcome(
                                        status=RunStatus.ABORTED,
                                        reason="spin_operator_aborted",
                                    ),
                                )
                                return run
                            if decision.action in {"modify", "re_decompose"}:
                                self._recovery_contexts[run.id] = {
                                    "provider_session_id": None,
                                    "lineage_id": None,
                                    "retry_cause": (
                                        f"hitl_{decision.action}:"
                                        f"{decision.instruction}"
                                    ),
                                }
                            self._strategy_executions[
                                run.id
                            ].terminal_outcome_seen = False
                            continue
                        raise RuntimeError(
                            f"unsupported spin control {request.action}"
                        ) from request
                    break
                if outcome is None:
                    outcome = StrategyOutcome(
                        status=RunStatus.FAILED,
                        reason="strategy_ended_without_terminal_outcome",
                    )
                if (
                    outcome.status == RunStatus.COMPLETED
                    and not outcome.details.get("_final_validated", False)
                ):
                    validation_result = await self._apply_final_validators(run, outcome)
                    if isinstance(validation_result, RunStatus):
                        if validation_result != RunStatus.PAUSED_HITL:
                            raise RuntimeError(
                                f"unexpected validator state: {validation_result.value}"
                            )
                        decision = await self.request_hitl(
                            run,
                            reason="validator_paused",
                            context={
                                "phase": "final",
                                "validated_outcome": outcome.model_dump(mode="json"),
                            },
                        )
                        if decision.action == "abort":
                            outcome = StrategyOutcome(
                                status=RunStatus.ABORTED,
                                reason="final_validator_operator_aborted",
                            )
                        elif decision.action in {"modify", "re_decompose"}:
                            outcome = StrategyOutcome(
                                status=RunStatus.FAILED,
                                reason=(
                                    "final_validator_operator_requested_"
                                    f"{decision.action}"
                                ),
                                details={
                                    "hitl_action": decision.action,
                                    "instruction": decision.instruction,
                                },
                            )
                    if not isinstance(validation_result, RunStatus):
                        outcome = validation_result
                await self._finish_run(run, outcome)
            except BudgetExceeded as exc:
                await self._finish_run(
                    run,
                    StrategyOutcome(
                        status=RunStatus.BUDGET_EXCEEDED,
                        reason="budget_exceeded",
                        details={"error": str(exc)},
                    ),
                )
                raise
            except TimeoutError:
                await self._finish_run(
                    run,
                    StrategyOutcome(
                        status=RunStatus.TIMED_OUT, reason="runtime_timeout"
                    ),
                )
                raise
            except Exception as exc:
                await self._finish_run(
                    run,
                    StrategyOutcome(
                        status=RunStatus.FAILED,
                        reason="runtime_error",
                        details={"error": str(exc)},
                    ),
                )
                raise
            finally:
                self._end_strategy_execution(run.id)
                self._recovery_contexts.pop(run.id, None)
                await self.store.save_run(run)
        return run

    async def _publish_strategy_switch(self, event: Event) -> tuple[Event, bool]:
        """Atomically claim the one durable switch, then notify live subscribers."""
        if isinstance(self.bus, DurableEventBus):
            persisted = cast(Event, await self.store.append_event(event))
            if persisted.id == event.id:
                await self.bus.downstream.publish(persisted)
            return persisted, persisted.id == event.id
        await self.bus.publish(event)
        return event, True

    async def request_spin_control(self, run: Run, action: str) -> bool:
        context = self._strategy_executions.get(run.id)
        if context is None or context.terminal_outcome_seen:
            return False
        if action == "terminate_session_and_retry":
            if context.retry_used:
                return False
            context.retry_used = True
            return True
        if action == "terminate_and_hitl":
            if context.retry_used:
                return False
            context.retry_used = True
            return True
        return False

    async def _begin_strategy_execution(self, run: Run, strategy: str) -> str:
        switched_events = await self.store.list_events(
            run.id, event_type="strategy.switched"
        )
        if switched_events:
            target = switched_events[-1].payload.get("to")
            if not isinstance(target, str) or not target:
                raise RuntimeError("durable strategy switch has no target")
            self._strategy_executions[run.id] = _StrategyExecution(
                current=target, switched=True
            )
            return target
        self._strategy_executions[run.id] = _StrategyExecution(current=strategy)
        return strategy

    def _end_strategy_execution(self, run_id: str) -> None:
        self._strategy_executions.pop(run_id, None)

    def pending_strategy_switch(self, run_id: str) -> str | None:
        context = self._strategy_executions.get(run_id)
        return context.requested if context is not None else None

    async def request_strategy_switch(
        self, run: Run, *, target: str | None = None
    ) -> bool:
        """Accept at most one distinct switch request during an active run."""
        context = self._strategy_executions.get(run.id)
        if (
            context is None
            or context.switched
            or context.requested is not None
            or context.terminal_outcome_seen
        ):
            return False
        selected = target or run.task.spin_detection.switch_strategy_to
        if selected is None:
            selected = "sequential" if context.current == "single" else "single"
        if selected == context.current:
            return False
        try:
            self._load_strategy(selected)
        except (ValueError, ImportError, AttributeError):
            return False
        context.requested = selected
        return True

    def _mark_strategy_switched(self, run: Run, target: str) -> None:
        context = self._strategy_executions.get(run.id)
        if context is None or context.requested != target or context.switched:
            raise RuntimeError("strategy switch is not pending")
        context.current = target
        context.requested = None
        context.switched = True

    async def _apply_final_validators(
        self, run: Run, outcome: StrategyOutcome
    ) -> StrategyOutcome | RunStatus:
        """Apply one final-validator policy for every strategy.

        No configured final validators is a vacuous pass. Otherwise every verdict must
        continue; pause delegates to the durable operator flow, abort ends it as
        aborted, and a retry request ends the current run as failed because a new
        attempt is required.
        """
        final_session = self._last_sessions.get(run.id)
        if final_session is None and hasattr(self.store, "list_sessions"):
            sessions = await self.store.list_sessions(run.id)
            final_session = sessions[-1] if sessions else None
        decisions = await self.run_validators(run, final_session, when="final")
        if not decisions or all(d.decision == GateAction.CONTINUE for d in decisions):
            return outcome
        actions = {decision.decision for decision in decisions}
        details = {"validator_actions": sorted(action.value for action in actions)}
        if GateAction.ABORT in actions:
            return StrategyOutcome(
                status=RunStatus.ABORTED,
                reason="final_validator_aborted",
                details=details,
            )
        if GateAction.PAUSE_FOR_HITL in actions:
            return RunStatus.PAUSED_HITL
        return StrategyOutcome(
            status=RunStatus.FAILED,
            reason="final_validator_requested_retry",
            details=details,
        )

    async def _finish_run(self, run: Run, outcome: StrategyOutcome) -> None:
        persisted = await self.store.transition_run(run.id, outcome.status)
        run.status = persisted.status
        run.completed_at = persisted.completed_at
        payload = {
            "status": run.status.value,
            "reason": outcome.reason,
            **{
                key: value
                for key, value in outcome.details.items()
                if not key.startswith("_")
            },
        }
        if run.status == RunStatus.COMPLETED:
            event = Event(type="run.completed", run_id=run.id, payload=payload)
        else:
            event = Event(
                type="run.failed",
                run_id=run.id,
                payload=payload,
            )
        await self.bus.publish(event)

    # ---------------------------------------------------------------
    # Session primitives — called by strategies
    # ---------------------------------------------------------------

    async def start_session(
        self,
        run: Run,
        target_goal: GoalNode | None = None,
        *,
        session_id: str | None = None,
    ) -> Session:
        max_sessions = run.task.resources.max_sessions
        if (
            max_sessions is not None
            and run.cumulative.sessions_count >= max_sessions
        ):
            raise BudgetExceeded(
                f"session limit reached: {run.cumulative.sessions_count}/{max_sessions}"
            )
        sequence = run.cumulative.sessions_count
        session = Session(
            id=session_id or new_session_id(),
            run_id=run.id,
            sequence_index=sequence,
            target_goal_id=target_goal.id if target_goal else None,
            status=SessionStatus.RUNNING,
        )
        self._last_sessions[run.id] = session
        run.current_session_id = session.id
        run.cumulative.sessions_count += 1
        await self.store.save_session(session)
        await self.store.save_run(run)
        await self.bus.publish(
            Event(
                type="session.started",
                run_id=run.id,
                session_id=session.id,
                payload={"target_goal": target_goal.id if target_goal else None},
            )
        )
        return session

    async def end_session(self, session: Session, status: SessionStatus) -> None:
        from horizonx.core.types import utcnow

        session.status = status
        session.completed_at = utcnow()
        await self.store.save_session(session)
        await self.bus.publish(
            Event(
                type="session.completed",
                run_id=session.run_id,
                session_id=session.id,
                payload={"status": status.value},
            )
        )

    async def record_step(self, session: Session, step: Step) -> None:
        from horizonx.core.housekeeping import is_housekeeping_step
        if is_housekeeping_step(step):
            session.housekeeping_steps += 1
        else:
            session.steps_count += 1
        await self.recorder.record(session, step)
        await self.store.save_session(session)
        governor = self._governors.get(session.run_id)
        if governor is not None:
            governor.checkpoint_wall_time()
            await self.store.save_run(governor.run)

    # ---------------------------------------------------------------
    # Validators
    # ---------------------------------------------------------------

    def _effective_validator_configs(
        self, run: Run, session: Session | None
    ) -> list[ValidatorConfig]:
        """Resolve validators for this validator pass — goal-graph aware (GE-03).

        Falls back to run.task.milestone_validators unchanged when there's no
        active goal (session is None / no target_goal_id) or no goals.json yet,
        preserving behavior for strategies that don't use a goal graph.
        """
        if session is None or not session.target_goal_id:
            return run.task.milestone_validators
        goals_path = run.workspace_path / "goals.json"
        if not goals_path.exists():
            return run.task.milestone_validators
        from horizonx.core.goal_graph import GoalGraph, GoalGraphError
        try:
            graph = GoalGraph.load(goals_path)
            return graph.effective_validators(session.target_goal_id, run.task.milestone_validators)
        except (GoalGraphError, KeyError, FileNotFoundError):
            return run.task.milestone_validators

    async def run_validators(
        self, run: Run, session: Session | None, *, when: str
    ) -> list[Any]:
        from horizonx.validators.registry import build_validator

        decisions = []
        for vc in self._effective_validator_configs(run, session):
            should_run = (
                vc.runs == when
                or (vc.runs == "every_n_sessions" and session and (session.sequence_index + 1) % (vc.n or 1) == 0)
            )
            if not should_run:
                continue
            validator = build_validator(vc, store=self.store)
            workspace = self._workspace_for(run)
            decision = await validator.validate(run, session, workspace)
            await self.store.save_validation(run, session, decision)
            ev_type = (
                "validator.passed" if decision.decision == GateAction.CONTINUE
                else "validator.paused" if decision.decision == GateAction.PAUSE_FOR_HITL
                else "validator.failed"
            )
            await self.bus.publish(
                Event(
                    type=ev_type,  # type: ignore[arg-type]
                    run_id=run.id,
                    session_id=session.id if session else None,
                    payload={"validator": vc.id, "reason": decision.reason},
                )
            )
            decisions.append(decision)
        return decisions

    # ---------------------------------------------------------------
    # Spin detection
    # ---------------------------------------------------------------

    async def check_spin(self, session: Session, run: Run) -> Any:
        if not run.task.spin_detection.enabled:
            return None
        # In-session layers
        detector = SpinDetector(config=run.task.spin_detection, store=self.store)
        report = await detector.check(session)
        # Cross-session layer (goal-graph progress check across multiple sessions)
        if not report.detected:
            report = await CrossSessionSpinLayer().check_cross_session(run.id, self.store)
        if report.detected and report.action != "warn_and_inject_diagnostic":
            action_map: dict[str, Any] = {
                "terminate_and_retry": "terminate_session_and_retry",
                "terminate_and_hitl": "terminate_and_hitl",
                "switch_strategy": "switch_strategy",
            }
            report.action = action_map[run.task.spin_detection.on_spin]
        if report.detected:
            await self.store.save_spin_report(session, report)
            await self.bus.publish(
                Event(
                    type="spin.detected",
                    run_id=session.run_id,
                    session_id=session.id,
                    payload={"layer": report.layer, "action": report.action},
                )
            )
        return report

    # ---------------------------------------------------------------
    # HITL
    # ---------------------------------------------------------------

    async def request_hitl(
        self, run: Run, *, reason: str, context: dict[str, Any]
    ) -> HITLDecision:
        cfg = run.task.hitl
        configured_trigger = reason
        if reason in {"validator_pause", "validator_or_spin"}:
            configured_trigger = (
                "spin_detected"
                if context.get("spin_reason")
                else "validator_paused"
            )
        if not cfg.enabled or (
            cfg.triggers and configured_trigger not in cfg.triggers
        ):
            return HITLDecision(
                action="approve",
                instruction=f"HITL skipped for unconfigured trigger: {reason}",
                operator="system:policy",
            )
        from horizonx.storage.sqlite import HITLTransitionError

        try:
            request_id, requested_event = await self.store.enter_hitl(
                run.id, reason, context, actor="system"
            )
        except HITLTransitionError:
            persisted = await self.store.load_run(run.id)
            run.status = persisted.status
            run.completed_at = persisted.completed_at
            return HITLDecision(
                action="abort",
                instruction=f"run became {persisted.status.value} before HITL entry",
                operator="system:operator-control",
            )
        run.status = RunStatus.PAUSED_HITL
        run.active_hitl_request_id = request_id
        if isinstance(self.bus, DurableEventBus):
            await self.bus.downstream.publish(requested_event)
        else:
            await self.bus.publish(requested_event)
        from horizonx.hitl.gate import await_decision

        decision = await await_decision(
            run,
            reason,
            context,
            cfg,
            store=self.store,
            request_id=request_id,
        )
        resolved_events = await self.store.list_events(
            run.id, event_type="hitl.resolved"
        )
        persisted_event = next(
            (event for event in resolved_events
             if event.id == f"hitl-resolved:{request_id}"), None
        )
        if persisted_event is not None:
            if isinstance(self.bus, DurableEventBus):
                await self.bus.downstream.publish(persisted_event)
            else:
                await self.bus.publish(persisted_event)
        target = (
            RunStatus.ABORTED if decision.action == "abort" else RunStatus.RUNNING
        )
        try:
            persisted = await self.store.apply_hitl_decision(
                run.id,
                expected_request_id=request_id,
                to_status=target,
            )
        except HITLTransitionError:
            persisted = await self.store.load_run(run.id)
            if persisted.status in TERMINAL_RUN_STATUSES:
                decision = HITLDecision(
                    action="abort",
                    instruction=(
                        f"run became {persisted.status.value} before "
                        "HITL decision application"
                    ),
                    operator="system:operator-control",
                )
            else:
                persisted = await self.store.transition_run(run.id, RunStatus.FAILED)
                decision = HITLDecision(
                    action="abort",
                    instruction="active HITL generation changed before consumption",
                    operator="system:operator-control",
                )
        run.status = persisted.status
        run.completed_at = persisted.completed_at
        run.active_hitl_request_id = persisted.active_hitl_request_id
        return decision

    # ---------------------------------------------------------------
    # Summarizer
    # ---------------------------------------------------------------

    async def summarize(self, session: Session, run: Run) -> Any:
        if not run.task.summarizer.enabled:
            return None
        summarizer = Summarizer(config=run.task.summarizer, store=self.store)
        summary = await summarizer.summarize(session, run)
        await self.bus.publish(
            Event(
                type="summary.created",
                run_id=run.id,
                session_id=session.id,
                payload={"path": str(summary)},
            )
        )
        return summary

    # ---------------------------------------------------------------
    # Workspace + governor
    # ---------------------------------------------------------------

    @asynccontextmanager
    async def _workspace_run_slot(self, task: Task) -> AsyncIterator[None]:
        workspace = task.workspace
        if workspace is None:
            yield
            return
        workspace_id = workspace.workspace_id
        async with self._workspace_run_lock:
            active = self._workspace_run_counts.get(workspace_id, 0)
            if active >= workspace.max_concurrent_runs:
                raise BudgetExceeded(
                    f"workspace {workspace_id!r} concurrent run limit reached: "
                    f"{active}/{workspace.max_concurrent_runs}"
                )
            self._workspace_run_counts[workspace_id] = active + 1
        try:
            yield
        finally:
            async with self._workspace_run_lock:
                remaining = self._workspace_run_counts[workspace_id] - 1
                if remaining:
                    self._workspace_run_counts[workspace_id] = remaining
                else:
                    self._workspace_run_counts.pop(workspace_id, None)

    def _workspace_for(self, run: Run) -> Any:
        from horizonx.environments.local import LocalWorkspace

        return LocalWorkspace(run.workspace_path, env=self.workspace_env(run))

    def workspace_env(self, run: Run) -> dict[str, str]:
        prepared = self._prepared_workspaces.get(run.id)
        return dict(prepared.env) if prepared is not None else {}

    def workspace_snapshot(self, run: Run) -> dict[str, Any]:
        prepared = self._prepared_workspaces.get(run.id)
        return prepared.metadata.to_dict() if prepared is not None else {}

    def take_recovery_context(self, run_id: str) -> dict[str, str | None] | None:
        """Consume recovery metadata once, for the first resumed agent attempt."""
        return self._recovery_contexts.pop(run_id, None)

    async def prepare_workspace(self, run: Run, *, resume: bool) -> PreparedWorkspace:
        backend = GitWorktreeBackend(self.workspace_root, run.task.environment)
        prepared = (
            await backend.resume(run.workspace_path)
            if resume
            else await backend.prepare(run.id, run.task.repository)
        )
        run.workspace_path = prepared.path
        self._prepared_workspaces[run.id] = prepared
        await self.store.save_run(run)
        return prepared

    async def checkpoint_resources(self, run: Run) -> None:
        governor = self._governors.get(run.id)
        if governor is not None:
            governor.checkpoint_wall_time()
            await self.store.save_run(run)

    async def charge(
        self,
        run: Run,
        result: Any,
        *,
        attempt_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        """Charge the governor belonging to *run* for one completed session."""
        governor = self._governors.get(run.id)
        if governor is not None and result is not None:
            try:
                await governor.charge(
                    tokens_in=getattr(result, "tokens_in", 0),
                    tokens_out=getattr(result, "tokens_out", 0),
                    cache_creation_tokens=getattr(
                        result, "cache_creation_tokens", 0
                    ),
                    cache_read_tokens=getattr(result, "cache_read_tokens", 0),
                    usd=getattr(result, "cost_usd", None),
                )
            finally:
                await self.bus.publish(
                    Event(
                        type="usage.charged",
                        run_id=run.id,
                        attempt_id=attempt_id,
                        session_id=session_id,
                        payload={
                            "tokens_in": getattr(result, "tokens_in", 0),
                            "tokens_out": getattr(result, "tokens_out", 0),
                            "cache_creation_tokens": getattr(
                                result, "cache_creation_tokens", 0
                            ),
                            "cache_read_tokens": getattr(
                                result, "cache_read_tokens", 0
                            ),
                            "usd": getattr(result, "cost_usd", None),
                            "usd_known": getattr(result, "cost_usd", None)
                            is not None,
                        },
                    )
                )
                await self.store.save_run(run)

    @asynccontextmanager
    async def _governor(self, run: Run) -> AsyncIterator[None]:
        usage_store = None
        velocity_monitor = None
        if run.task.workspace is not None:
            from horizonx.core.usage import CostVelocityMonitor, UsageStore
            usage_store = UsageStore(self.store)
            velocity_monitor = CostVelocityMonitor()
        gov = ResourceGovernor(
            run.task.resources, run, self.bus,
            hitl_callback=self.request_hitl,
            usage_store=usage_store,
            velocity_monitor=velocity_monitor,
        )
        if run.id in self._governors:
            raise RuntimeError(f"resource governor already active for run {run.id}")
        self._governors[run.id] = gov
        try:
            async with gov:
                yield
        finally:
            self._governors.pop(run.id, None)

    # ---------------------------------------------------------------
    # Loading
    # ---------------------------------------------------------------

    async def _load_or_create(self, task: Task, resume_from: str | None) -> Run:
        if resume_from:
            run = await self.store.load_run(resume_from)
            if run.status in TERMINAL_RUN_STATUSES:
                raise ValueError(
                    f"cannot resume terminal run {run.id} with status {run.status.value}; "
                    "fork it to start new work"
                )
            if task != run.task:
                raise ValueError(
                    f"resume task snapshot does not match persisted run {run.id}; "
                    "fork it to change task configuration"
                )
            await self._reconcile_cumulative(run)
            run.status = RunStatus.RUNNING
            return cast(Run, run)
        run_id = new_run_id()
        workspace = self.workspace_root / run_id
        return Run(
            id=run_id,
            task=task,
            workspace_path=workspace,
            status=RunStatus.RUNNING,
        )

    async def _reconcile_cumulative(self, run: Run) -> None:
        """Repair aggregate counters from durable session and attempt records."""
        sessions: list[Session] = []
        if hasattr(self.store, "list_sessions"):
            sessions = await self.store.list_sessions(run.id)
            run.cumulative.sessions_count = len(sessions)
            run.cumulative.steps_count = sum(s.steps_count for s in sessions)
            run.cumulative.housekeeping_steps = sum(
                s.housekeeping_steps for s in sessions
            )
        if hasattr(self.store, "list_attempts"):
            attempts = await self.store.list_attempts(run.id)
            run.cumulative.attempts_count = len(attempts)
        if hasattr(self.store, "list_events"):
            usage_events = await self.store.list_events(
                run.id, limit=100_000, event_type="usage.charged"
            )
            if usage_events:
                run.cumulative.tokens_in = sum(
                    int(event.payload.get("tokens_in") or 0)
                    for event in usage_events
                )
                run.cumulative.tokens_out = sum(
                    int(event.payload.get("tokens_out") or 0)
                    for event in usage_events
                )
                run.cumulative.cache_creation_tokens = sum(
                    int(event.payload.get("cache_creation_tokens") or 0)
                    for event in usage_events
                )
                run.cumulative.cache_read_tokens = sum(
                    int(event.payload.get("cache_read_tokens") or 0)
                    for event in usage_events
                )
                run.cumulative.cost_known = all(
                    bool(event.payload.get("usd_known"))
                    for event in usage_events
                )
                run.cumulative.usd = (
                    sum(float(event.payload.get("usd") or 0.0) for event in usage_events)
                    if run.cumulative.cost_known
                    else None
                )
                cache_eligible = (
                    run.cumulative.tokens_in
                    + run.cumulative.cache_read_tokens
                )
                run.cumulative.cache_hit_rate = (
                    run.cumulative.cache_read_tokens / cache_eligible
                    if cache_eligible
                    else 0.0
                )
            charged_sessions = {
                event.session_id for event in usage_events if event.session_id
            }
            partial_tokens_in = 0
            partial_tokens_out = 0
            partial_cache_creation = 0
            partial_cache_read = 0
            active_wall_seconds = 0.0
            for session in sessions:
                steps = await self.store.recent_steps(session.id, 100_000)
                if session.completed_at is not None:
                    active_wall_seconds += max(
                        0.0,
                        (session.completed_at - session.started_at).total_seconds(),
                    )
                elif steps:
                    active_wall_seconds += max(
                        0.0,
                        (steps[-1].timestamp - session.started_at).total_seconds(),
                    )
                if session.id in charged_sessions:
                    continue
                session_tokens_in = 0
                session_tokens_out = 0
                session_cache_creation = 0
                session_cache_read = 0
                for step in steps:
                    if step.type != StepType.USAGE:
                        continue
                    usage = step.content.get("usage") or step.content
                    if not isinstance(usage, dict):
                        continue
                    observed_tokens_in = int(usage.get("input_tokens") or 0)
                    observed_tokens_out = int(usage.get("output_tokens") or 0)
                    observed_cache_creation = int(
                        usage.get("cache_creation_input_tokens") or 0
                    )
                    observed_cache_read = int(
                        usage.get("cache_read_input_tokens")
                        or usage.get("cached_input_tokens")
                        or 0
                    )
                    if step.content.get("usage_mode") == "cumulative":
                        session_tokens_in = observed_tokens_in
                        session_tokens_out = observed_tokens_out
                        session_cache_creation = observed_cache_creation
                        session_cache_read = observed_cache_read
                    else:
                        session_tokens_in += observed_tokens_in
                        session_tokens_out += observed_tokens_out
                        session_cache_creation += observed_cache_creation
                        session_cache_read += observed_cache_read
                partial_tokens_in += session_tokens_in
                partial_tokens_out += session_tokens_out
                partial_cache_creation += session_cache_creation
                partial_cache_read += session_cache_read
            run.cumulative.tokens_in += partial_tokens_in
            run.cumulative.tokens_out += partial_tokens_out
            run.cumulative.cache_creation_tokens += partial_cache_creation
            run.cumulative.cache_read_tokens += partial_cache_read
            if partial_tokens_in or partial_tokens_out:
                run.cumulative.cost_known = False
                run.cumulative.usd = None
            cache_eligible = (
                run.cumulative.tokens_in + run.cumulative.cache_read_tokens
            )
            run.cumulative.cache_hit_rate = (
                run.cumulative.cache_read_tokens / cache_eligible
                if cache_eligible
                else 0.0
            )
            run.cumulative.wall_seconds = max(
                run.cumulative.wall_seconds, active_wall_seconds
            )
        await self.store.save_run(run)

    # ---------------------------------------------------------------
    # Fork / Merge
    # ---------------------------------------------------------------

    async def fork_run(self, parent_run_id: str, *, strategy_override: Any = None) -> Run:
        """Fork an existing run at its current state.

        Creates a new Run with parent_run_id set, copies the workspace snapshot
        (handoff files + goals.json), and resets status to RUNNING. The fork
        can run a different strategy or agent config to explore an alternative path.

        Returns the new forked Run (not yet persisted to store — caller must await rt.run()).
        """
        import shutil

        parent = await self.store.load_run(parent_run_id)
        fork_workspace = self.workspace_root / f"{parent.task.id}-fork-{new_session_id()[:8]}"
        fork_workspace.mkdir(parents=True, exist_ok=True)

        # Copy handoff files from parent workspace
        for fname in parent.task.handoff_files:
            src = parent.workspace_path / fname
            if src.exists():
                shutil.copy2(src, fork_workspace / fname)

        fork_task = parent.task.model_copy(deep=True)
        if strategy_override:
            fork_task.strategy = strategy_override

        fork = Run(
            parent_run_id=parent_run_id,
            task=fork_task,
            workspace_path=fork_workspace,
            status=RunStatus.RUNNING,
        )
        await self.store.save_run(fork)
        await self.bus.publish(Event(type="fork.created", run_id=fork.id,
                                    payload={"parent_run_id": parent_run_id}))
        return fork

    async def merge_run(self, fork_run_id: str, into_run_id: str) -> None:
        """Merge a fork's goal graph progress back into the parent run.

        Uses a simple last-write-wins merge on individual goals: a goal that
        is DONE in the fork is marked DONE in the parent (never regressed).
        Notes are concatenated. The fork's workspace handoff files are NOT
        merged — only the goal graph state is transferred.
        """
        fork = await self.store.load_run(fork_run_id)
        parent = await self.store.load_run(into_run_id)

        fork_goals_path = fork.workspace_path / "goals.json"
        parent_goals_path = parent.workspace_path / "goals.json"

        if not fork_goals_path.exists() or not parent_goals_path.exists():
            return  # Nothing to merge

        from horizonx.core.goal_graph import GoalGraph
        from horizonx.core.types import GoalStatus

        fork_graph = GoalGraph.load(fork_goals_path)
        parent_graph = GoalGraph.load(parent_goals_path)

        merged = False
        for node_id, fork_node in fork_graph._nodes.items():
            if node_id not in parent_graph._nodes:
                continue
            parent_node = parent_graph._nodes[node_id]
            # Promote status if fork made more progress
            status_rank = {
                GoalStatus.PENDING: 0,
                GoalStatus.BLOCKED: 0,
                GoalStatus.IN_PROGRESS: 1,
                GoalStatus.FAILED: 1,
                GoalStatus.SKIPPED: 1,
                GoalStatus.DONE: 2,
            }
            if status_rank.get(fork_node.status, 0) > status_rank.get(parent_node.status, 0):
                parent_node.status = fork_node.status
                parent_node.progress_pct = max(parent_node.progress_pct, fork_node.progress_pct)
                parent_node.version += 1
                merged = True
            if fork_node.notes and fork_node.notes not in (parent_node.notes or ""):
                sep = "\n\n[fork merge]\n" if parent_node.notes else ""
                parent_node.notes = f"{parent_node.notes}{sep}{fork_node.notes}"
                parent_node.version += 1
                merged = True

        if merged:
            parent_graph.save(parent_goals_path)
            await self.store.save_run(parent)

        await self.bus.publish(Event(type="fork.merged", run_id=into_run_id,
                                    payload={"fork_run_id": fork_run_id, "merged": merged}))

    @staticmethod
    def _load_strategy(kind: str) -> Any:
        entry_points = list(
            importlib.metadata.entry_points(group="horizonx.strategies")
        )
        for ep in entry_points:
            if ep.name == kind:
                return ep.load()
        target = _BUILTIN_STRATEGIES.get(kind)
        if target is not None:
            module_name, object_name = target.split(":", maxsplit=1)
            return getattr(importlib.import_module(module_name), object_name)
        available = sorted(
            _BUILTIN_STRATEGIES.keys() | {entry.name for entry in entry_points}
        )
        raise ValueError(
            f"unknown strategy {kind!r}; available: {', '.join(available)}"
        )
