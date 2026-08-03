"""Runtime — the central orchestrator.

See docs/LONG_HORIZON_AGENT.md §11.
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

from horizonx.core.event_bus import DurableEventBus, Event, EventBus, InMemoryBus
from horizonx.core.governor import BudgetExceeded, ResourceGovernor
from horizonx.core.recorder import TrajectoryRecorder
from horizonx.core.spin_detector import CrossSessionSpinLayer, SpinDetector
from horizonx.core.summarizer import Summarizer
from horizonx.core.types import (
    GateAction,
    GoalNode,
    HITLDecision,
    Run,
    RunStatus,
    Session,
    SessionStatus,
    Step,
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
        self._governor_ref: Any = None  # set while a run is active
        self._last_sessions: dict[str, Session] = {}
        self._prepared_workspaces: dict[str, PreparedWorkspace] = {}
        self._recovery_contexts: dict[str, dict[str, str | None]] = {}

    async def __aenter__(self) -> Runtime:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if hasattr(self.store, "close"):
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
        # Pre-flight workspace daily budget check
        if task.workspace and task.workspace.daily_budget_usd is not None:
            from horizonx.core.usage import UsageStore
            spent = await UsageStore(self.store).daily_usd(task.workspace.workspace_id)
            if spent >= task.workspace.daily_budget_usd:
                raise BudgetExceeded(
                    f"workspace {task.workspace.workspace_id!r} daily budget "
                    f"${task.workspace.daily_budget_usd:.2f} already spent (${spent:.2f} today)"
                )
        # Resolve and validate execution code before creating a run or touching a repository.
        strategy_cls = self._load_strategy(task.strategy.kind)
        strategy = strategy_cls(task.strategy.config)
        run = await self._load_or_create(task, resume_from)
        await self.store.save_run(run)
        try:
            workspace_metadata = run.workspace_path / ".horizonx" / "workspace.json"
            await self.prepare_workspace(
                run,
                resume=resume_from is not None and workspace_metadata.is_file(),
            )
        except WorkspaceError as exc:
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
                return run

        if recovery_lineage_id or resume_provider_session_id or retry_cause:
            self._recovery_contexts[run.id] = {
                "provider_session_id": resume_provider_session_id,
                "lineage_id": recovery_lineage_id,
                "retry_cause": retry_cause,
            }

        async with self._governor(run):
            try:
                outcome: StrategyOutcome | None = None
                async for item in strategy.execute(run, self):
                    if isinstance(item, StrategyOutcome):
                        if outcome is not None:
                            raise RuntimeError("strategy yielded more than one terminal outcome")
                        outcome = item
                        continue
                    if outcome is not None:
                        raise RuntimeError("strategy yielded an event after its terminal outcome")
                    await self.bus.publish(item)
                    # Re-run decomposition check after graph is first written
                    if not goals_path.exists():
                        pass
                    elif not hasattr(run, "decomposition_report") or not run.decomposition_report:
                        from horizonx.core.decomposition_checker import DecompositionChecker
                        rpt = DecompositionChecker().check_file(goals_path, task)
                        run.decomposition_report = rpt.to_dict()
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
                        await self._pause_run(run)
                        return run
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
                self._recovery_contexts.pop(run.id, None)
                await self.store.save_run(run)
        return run

    async def _apply_final_validators(
        self, run: Run, outcome: StrategyOutcome
    ) -> StrategyOutcome | RunStatus:
        """Apply one final-validator policy for every strategy.

        No configured final validators is a vacuous pass. Otherwise every verdict must
        continue; pause leaves the run paused, abort ends it as aborted, and a retry
        request ends the current run as failed because a new attempt is required.
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

    async def _pause_run(self, run: Run) -> None:
        """Persist a resumable operator pause without assigning a completion time."""
        run.status = RunStatus.PAUSED_HITL
        run.completed_at = None
        await self.store.save_run(run)
        persisted = await self.store.load_run(run.id)
        run.status = persisted.status
        run.completed_at = persisted.completed_at
        if run.status == RunStatus.PAUSED_HITL:
            await self.bus.publish(
                Event(
                    type="run.paused_hitl",
                    run_id=run.id,
                    payload={
                        "status": run.status.value,
                        "reason": "final_validator_requires_operator",
                    },
                )
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
        run.status = RunStatus.PAUSED_HITL
        await self.store.save_run(run)
        await self.bus.publish(
            Event(
                type="hitl.requested",
                run_id=run.id,
                payload={"reason": reason, "context": context},
            )
        )
        from horizonx.hitl.gate import await_decision

        decision = await await_decision(run, reason, context, run.task.hitl)
        await self.bus.publish(
            Event(
                type="hitl.resolved",
                run_id=run.id,
                payload={"action": decision.action, "instruction": decision.instruction},
            )
        )
        run.status = RunStatus.RUNNING
        await self.store.save_run(run)
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

    def charge(self, result: Any) -> None:
        """Charge the active governor for a completed session. Call after agent.run_session()."""
        if self._governor_ref is not None and result is not None:
            self._governor_ref.charge(
                tokens_in=getattr(result, "tokens_in", 0),
                tokens_out=getattr(result, "tokens_out", 0),
                usd=getattr(result, "cost_usd", 0.0),
            )

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
        self._governor_ref = gov
        try:
            async with gov:
                yield
        finally:
            self._governor_ref = None

    # ---------------------------------------------------------------
    # Loading
    # ---------------------------------------------------------------

    async def _load_or_create(self, task: Task, resume_from: str | None) -> Run:
        if resume_from:
            run = await self.store.load_run(resume_from)
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
