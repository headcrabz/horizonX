"""SequentialSubgoals — the Anthropic pattern.

One sub-goal per session. Filesystem handoffs. Mandatory checklists.
See docs/LONG_HORIZON_AGENT.md §21.2.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from horizonx.agents.base import CancelToken, Workspace
from horizonx.agents.claude_code import ClaudeCodeAgent
from horizonx.agents.codex import CodexAgent
from horizonx.core.event_bus import Event
from horizonx.core.goal_graph import GoalGraph
from horizonx.core.session_manager import SessionManager
from horizonx.core.types import (
    GateAction,
    GoalNode,
    GoalStatus,
    Run,
    SessionStatus,
)


def _build_agent(ac: Any):
    if ac.type == "claude_code":
        return ClaudeCodeAgent(ac)
    if ac.type == "codex":
        return CodexAgent(ac)
    if ac.type == "custom":
        from horizonx.agents.custom import CustomAgent
        return CustomAgent(ac)
    if ac.type == "mock":
        from horizonx.agents.mock import MockAgent
        return MockAgent(config=ac)
    raise ValueError(f"unknown agent type for SequentialSubgoals: {ac.type}")


class SequentialSubgoals:
    kind = "sequential"

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.max_attempts_per_goal = config.get("max_attempts_per_goal", 3)
        self.target_subgoals = config.get("target_subgoals", [40, 80])
        self.git_commit_each_session = config.get("git_commit_each_session", True)

    async def execute(self, run: Run, rt: Any) -> AsyncIterator[Event]:
        graph_path = run.workspace_path / "goals.json"

        # Phase 1 — Initializer (if no goal graph yet)
        if not graph_path.exists():
            yield Event(type="run.started", run_id=run.id, payload={"phase": "initializer"})
            await self._run_initializer(run, rt)

        # Load (or reload) the graph
        graph = GoalGraph.load(graph_path)

        # Phase 2 — Iterate sub-goals
        while True:
            goal = graph.next_pending_leaf()
            if goal is None:
                break  # all done or all blocked

            await self._run_goal_session(run, rt, graph, goal)
            graph.save(graph_path)
            yield Event(
                type="goal.in_progress" if goal.status != GoalStatus.DONE else "goal.done",
                run_id=run.id,
                payload={"goal_id": goal.id, "status": goal.status.value, "attempts": goal.attempts},
            )

            if graph.is_complete():
                break

        if graph.is_complete():
            yield Event(type="run.completed", run_id=run.id, payload={"strategy": "sequential"})
        else:
            yield Event(type="run.failed", run_id=run.id, payload={"reason": "goal_graph_incomplete"})

    # ------------------------------------------------------------------
    # Phase 1 — Initializer
    # ------------------------------------------------------------------

    async def _run_initializer(self, run: Run, rt: Any) -> None:
        session = await rt.start_session(run, target_goal=None)
        sm = SessionManager(run)
        prompt = sm.compose_prompt(target_goal=None)
        agent = _build_agent(run.task.agent)
        workspace = Workspace(path=run.workspace_path, env={})

        async def on_step(step):
            step.session_id = session.id
            await rt.record_step(session, step)

        cancel_token = CancelToken()
        result = await agent.run_session(
            session_prompt=prompt,
            workspace=workspace,
            on_step=on_step,
            cancel_token=cancel_token,
            session_id=session.id,
        )
        if result.agent_session_id:
            session.agent_session_id = result.agent_session_id

        # Verify goals.json was created; if not, we cannot continue.
        graph_path = run.workspace_path / "goals.json"
        if not graph_path.exists():
            self._write_default_graph(run)

        # Initial git commit so subsequent sessions have a baseline
        self._git_init_and_commit(run.workspace_path, message="initializer: scaffold")
        await rt.end_session(session, result.status or SessionStatus.COMPLETED)

    def _write_default_graph(self, run: Run) -> None:
        """Fallback: if the initializer didn't write goals.json, create one with the root only."""
        graph = GoalGraph.empty(
            root_name=run.task.name,
            root_description=run.task.description or run.task.prompt[:500],
        )
        graph.save(run.workspace_path / "goals.json")

    def _git_init_and_commit(self, workspace: Path, message: str) -> None:
        try:
            if not (workspace / ".git").exists():
                subprocess.run(["git", "init"], cwd=workspace, check=False, capture_output=True)
                subprocess.run(["git", "config", "user.email", "horizonx@local"], cwd=workspace, check=False, capture_output=True)
                subprocess.run(["git", "config", "user.name", "HorizonX"], cwd=workspace, check=False, capture_output=True)
            subprocess.run(["git", "add", "-A"], cwd=workspace, check=False, capture_output=True)
            subprocess.run(["git", "commit", "-m", message], cwd=workspace, check=False, capture_output=True)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Phase 2 — Per-goal session
    # ------------------------------------------------------------------

    async def _run_goal_session(self, run: Run, rt: Any, graph: GoalGraph, goal: GoalNode) -> None:
        graph.mark_in_progress(goal.id, by_session="pending")
        graph.save(run.workspace_path / "goals.json")

        session = await rt.start_session(run, target_goal=goal)
        graph.mark_in_progress(goal.id, by_session=session.id)
        graph.save(run.workspace_path / "goals.json")

        sm = SessionManager(run)
        prompt = sm.compose_prompt(target_goal=goal)
        agent = _build_agent(run.task.agent)
        workspace = Workspace(path=run.workspace_path, env={})

        cancel_token = CancelToken()

        async def on_step(step):
            step.session_id = session.id
            await rt.record_step(session, step)
            # Mid-session spin check every N steps
            if session.steps_count > 0 and session.steps_count % 5 == 0:
                report = await rt.check_spin(session, run)
                if report and report.detected:
                    cancel_token.cancel(reason=f"spin:{report.layer}")

        result = await agent.run_session(
            session_prompt=prompt,
            workspace=workspace,
            resume_session_id=session.agent_session_id,
            on_step=on_step,
            cancel_token=cancel_token,
            session_id=session.id,
        )
        if result.agent_session_id:
            session.agent_session_id = result.agent_session_id

        # Auto git commit after session
        if self.git_commit_each_session:
            self._git_init_and_commit(run.workspace_path, message=f"session: {goal.id}")

        # Run validators after session
        decisions = await rt.run_validators(run, session, when="after_every_session")

        # Decide goal outcome
        spin_cancelled = cancel_token.cancelled and "spin" in cancel_token.reason
        any_pause = any(d.decision == GateAction.PAUSE_FOR_HITL for d in decisions)
        any_abort = any(d.decision == GateAction.ABORT for d in decisions)
        all_continue = all(d.decision == GateAction.CONTINUE for d in decisions) if decisions else True

        if any_abort:
            graph.mark_failed(goal.id, by_session=session.id)
            await rt.end_session(session, SessionStatus.ERRORED)
            return

        if spin_cancelled or any_pause:
            ctx = {
                "goal_id": goal.id,
                "spin_reason": cancel_token.reason if spin_cancelled else None,
                "validator_decisions": [d.model_dump() for d in decisions],
            }
            decision = await rt.request_hitl(run, reason="validator_or_spin", context=ctx)
            if decision.action == "abort":
                graph.mark_failed(goal.id, by_session=session.id)
                await rt.end_session(session, SessionStatus.ERRORED)
                return
            if decision.action == "modify":
                graph.append_notes(goal.id, f"HITL guidance: {decision.instruction}", by_session=session.id)
            if decision.action == "re_decompose":
                # Mark goal for re-decomposition by spawning child goals on the next turn
                graph.append_notes(goal.id, "HITL: re-decompose requested", by_session=session.id)
            # Continue the loop (do not mark done)
            await rt.end_session(session, result.status or SessionStatus.COMPLETED)
            return

        if all_continue:
            graph.mark_done(goal.id, by_session=session.id)
        else:
            if goal.attempts >= goal.max_attempts:
                graph.mark_failed(goal.id, by_session=session.id)

        await rt.end_session(session, result.status or SessionStatus.COMPLETED)
