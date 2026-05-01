"""Runtime — the central orchestrator.

See docs/LONG_HORIZON_AGENT.md §11.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from horizonx.core.event_bus import Event, EventBus, InMemoryBus
from horizonx.core.governor import ResourceGovernor
from horizonx.core.recorder import TrajectoryRecorder
from horizonx.core.spin_detector import SpinDetector
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
    Task,
    new_session_id,
)


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
        self.bus: EventBus = bus or InMemoryBus()
        self.workspace_root = workspace_root
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.recorder = TrajectoryRecorder(store=store, bus=self.bus)

    # ---------------------------------------------------------------
    # Top-level entry
    # ---------------------------------------------------------------

    async def run(self, task: Task, *, resume_from: str | None = None) -> Run:
        run = await self._load_or_create(task, resume_from)
        await self.store.save_run(run)
        await self.bus.publish(Event(type="run.started", run_id=run.id))

        strategy_cls = self._load_strategy(task.strategy.kind)
        strategy = strategy_cls(task.strategy.config)

        async with self._governor(run):
            try:
                async for event in strategy.execute(run, self):
                    await self.bus.publish(event)
                run.status = RunStatus.COMPLETED
                await self.bus.publish(Event(type="run.completed", run_id=run.id))
            except Exception as exc:
                run.status = RunStatus.FAILED
                await self.bus.publish(
                    Event(type="run.failed", run_id=run.id, payload={"error": str(exc)})
                )
                raise
            finally:
                await self.store.save_run(run)
        return run

    # ---------------------------------------------------------------
    # Session primitives — called by strategies
    # ---------------------------------------------------------------

    async def start_session(
        self, run: Run, target_goal: GoalNode | None = None
    ) -> Session:
        sequence = run.cumulative.sessions_count
        session = Session(
            run_id=run.id,
            sequence_index=sequence,
            target_goal_id=target_goal.id if target_goal else None,
            status=SessionStatus.RUNNING,
        )
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
        session.steps_count += 1
        await self.recorder.record(session, step)

    # ---------------------------------------------------------------
    # Validators
    # ---------------------------------------------------------------

    async def run_validators(
        self, run: Run, session: Session | None, *, when: str
    ) -> list[Any]:
        from horizonx.validators.registry import build_validator

        decisions = []
        for vc in run.task.milestone_validators:
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
                    type=ev_type,
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
        detector = SpinDetector(config=run.task.spin_detection, store=self.store)
        report = await detector.check(session)
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

        return LocalWorkspace(run.workspace_path)

    @asynccontextmanager
    async def _governor(self, run: Run) -> AsyncIterator[None]:
        gov = ResourceGovernor(run.task.resources, run, self.bus)
        async with gov:
            yield

    # ---------------------------------------------------------------
    # Loading
    # ---------------------------------------------------------------

    async def _load_or_create(self, task: Task, resume_from: str | None) -> Run:
        if resume_from:
            run = await self.store.load_run(resume_from)
            run.status = RunStatus.RUNNING
            return run
        workspace = self.workspace_root / f"{task.id}-{new_session_id()[:8]}"
        workspace.mkdir(parents=True, exist_ok=True)
        return Run(task=task, workspace_path=workspace, status=RunStatus.RUNNING)

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
        # Look up via entry points
        for ep in importlib.metadata.entry_points(group="horizonx.strategies"):
            if ep.name == kind:
                return ep.load()
        # Fallback to direct import (for dev without install)
        module = importlib.import_module(f"horizonx.strategies.{kind}")
        # First class in module that ends with strategy-ish names
        for name in dir(module):
            cls = getattr(module, name)
            if isinstance(cls, type) and name not in ("Strategy", "BaseModel", "Path"):
                if hasattr(cls, "execute"):
                    return cls
        raise ValueError(f"unknown strategy: {kind}")
