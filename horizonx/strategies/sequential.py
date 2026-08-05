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

from horizonx.core.attempt_executor import AttemptExecutor
from horizonx.core.event_bus import Event
from horizonx.core.goal_graph import GoalGraph
from horizonx.core.knowledge import RunKnowledgeStore
from horizonx.core.llm_client import call_llm_json
from horizonx.core.session_manager import SessionManager
from horizonx.core.task_board import append_task_board
from horizonx.core.types import (
    GateAction,
    GoalNode,
    GoalStatus,
    Run,
    RunStatus,
    SessionStatus,
    StrategyOutcome,
    new_session_id,
)


class SequentialSubgoals:
    kind = "sequential"

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.max_attempts_per_goal = config.get("max_attempts_per_goal", 3)
        self.target_subgoals = config.get("target_subgoals", [40, 80])
        self.git_commit_each_session = config.get("git_commit_each_session", True)

    async def execute(self, run: Run, rt: Any) -> AsyncIterator[Event | StrategyOutcome]:
        graph_path = run.workspace_path / "goals.json"

        # Phase 1 — Initializer (if no goal graph yet)
        if not graph_path.exists():
            yield Event(type="run.started", run_id=run.id, payload={"phase": "initializer"})
            await self._run_initializer(run, rt)

        # Commit an initializer-produced graph once, then treat SQLite as authoritative.
        graph = await rt.store.load_graph(run.id)
        if graph is None:
            graph = GoalGraph.load(graph_path)
            await rt.store.create_graph(run.id, graph)
        await rt.store.ensure_goal_projection(run.id, graph_path)

        # Phase 2 — Iterate sub-goals
        final_validated = False
        while True:
            goal = graph.next_pending_leaf()
            if goal is None:
                break  # all done or all blocked

            claimed, goal_final_validated, terminal_override = await self._run_goal_session(
                run, rt, graph, goal
            )
            final_validated = final_validated or goal_final_validated
            if not claimed:
                yield StrategyOutcome(
                    status=RunStatus.FAILED, reason="goal_claim_unavailable"
                )
                return
            await rt.store.create_graph(run.id, graph)
            await rt.store.ensure_goal_projection(run.id, graph_path)
            if terminal_override is not None:
                yield terminal_override
                return
            yield Event(
                type="goal.in_progress" if goal.status != GoalStatus.DONE else "goal.done",
                run_id=run.id,
                payload={"goal_id": goal.id, "status": goal.status.value, "attempts": goal.attempts},
            )

            if graph.is_complete():
                break

        if graph.is_complete():
            yield StrategyOutcome(
                status=RunStatus.COMPLETED,
                details={"_final_validated": final_validated},
            )
        else:
            yield StrategyOutcome(
                status=RunStatus.FAILED, reason="goal_graph_incomplete"
            )

    # ------------------------------------------------------------------
    # Phase 1 — Initializer
    # ------------------------------------------------------------------

    async def _run_initializer(self, run: Run, rt: Any) -> None:
        sm = SessionManager(run)
        prompt = sm.compose_prompt(target_goal=None)
        attempt = await AttemptExecutor(rt).execute(run, prompt=prompt)
        if not attempt.succeeded:
            raise RuntimeError(f"initializer ended with {attempt.status.value}")

        # Verify goals.json was created; if not, we cannot continue.
        graph_path = run.workspace_path / "goals.json"
        if not graph_path.exists():
            self._write_default_graph(run)

        # Initial git commit so subsequent sessions have a baseline
        self._git_init_and_commit(run.workspace_path, message="Initialize task workspace")

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

    async def _run_goal_session(
        self, run: Run, rt: Any, graph: GoalGraph, goal: GoalNode
    ) -> tuple[bool, bool, StrategyOutcome | None]:
        # Atomically claim in DB before mutating in-memory graph.
        # Returns False only if a concurrent agent already claimed this goal
        # (shouldn't happen in sequential strategy, but guards future parallel use).
        session_id = new_session_id()
        claimed = await rt.store.claim_goal(run.id, goal.id, session_id=session_id)
        if not claimed:
            return False, False, None

        graph.mark_in_progress(goal.id, by_session=session_id)
        goal.assigned_to_session = session_id
        await rt.store.create_graph(run.id, graph)
        await rt.store.ensure_goal_projection(run.id, run.workspace_path / "goals.json")

        append_task_board(
            run.workspace_path,
            event="claimed",
            goal_id=goal.id,
            session_id=session_id,
            agent_type=run.task.agent.type,
        )

        sm = SessionManager(run)
        prompt = sm.compose_prompt(target_goal=goal)
        is_final_goal = all(
            leaf.id == goal.id or leaf.status == GoalStatus.DONE
            for leaf in graph.leaves()
        )
        validator_stages = (
            ("after_every_session", "final")
            if is_final_goal
            else ("after_every_session",)
        )
        attempt = await AttemptExecutor(rt).execute(
            run,
            prompt=prompt,
            target_goal=goal,
            session_id=session_id,
            validator_stages=validator_stages,
        )
        session = attempt.session
        result = attempt.agent
        if not attempt.succeeded:
            graph.mark_failed(goal.id, by_session=session.id)
            goal.assigned_to_session = None
            await rt.store.release_goal(run.id, goal.id)
            append_task_board(
                run.workspace_path,
                event="failed",
                goal_id=goal.id,
                session_id=session.id,
                agent_type=run.task.agent.type,
            )
            return (
                True,
                False,
                StrategyOutcome(
                    status=(
                        RunStatus.TIMED_OUT
                        if result.status == SessionStatus.TIMEOUT
                        else RunStatus.FAILED
                    ),
                    reason=f"agent_{result.status.value}",
                    details={"goal_id": goal.id, "error": result.error},
                ),
            )

        # Auto git commit after session
        if self.git_commit_each_session:
            self._git_init_and_commit(
                run.workspace_path, message=f"Complete session for {goal.id}"
            )

        # Keep FTS5 decision index current for next session's context injection
        RunKnowledgeStore(run.workspace_path).index_decisions(
            run.workspace_path / "decisions.jsonl"
        )

        decisions = attempt.decisions
        final_validated = is_final_goal

        # Decide goal outcome
        spin_cancelled = attempt.spin_detected
        any_pause = any(d.decision == GateAction.PAUSE_FOR_HITL for d in decisions)
        any_abort = any(d.decision == GateAction.ABORT for d in decisions)
        all_continue = all(d.decision == GateAction.CONTINUE for d in decisions) if decisions else True

        if any_abort:
            graph.mark_failed(goal.id, by_session=session.id)
            goal.assigned_to_session = None
            await rt.store.release_goal(run.id, goal.id)
            append_task_board(run.workspace_path, event="failed", goal_id=goal.id,
                              session_id=session.id, agent_type=run.task.agent.type)
            return (
                True,
                final_validated,
                StrategyOutcome(
                    status=RunStatus.ABORTED,
                    reason="validator_aborted",
                    details={"goal_id": goal.id},
                ),
            )

        if spin_cancelled and attempt.spin_action == "terminate_session_and_retry":
            return False, final_validated, None

        if spin_cancelled or any_pause:
            ctx = {
                "goal_id": goal.id,
                "spin_reason": attempt.spin_action if spin_cancelled else None,
                "validator_decisions": [d.model_dump() for d in decisions],
            }
            decision = await rt.request_hitl(run, reason="validator_or_spin", context=ctx)
            if decision.action == "abort":
                graph.mark_failed(goal.id, by_session=session.id)
                goal.assigned_to_session = None
                await rt.store.release_goal(run.id, goal.id)
                append_task_board(run.workspace_path, event="failed", goal_id=goal.id,
                                  session_id=session.id, agent_type=run.task.agent.type)
                return (
                    True,
                    final_validated,
                    StrategyOutcome(
                        status=RunStatus.ABORTED,
                        reason="operator_aborted",
                        details={"goal_id": goal.id},
                    ),
                )
            if decision.action == "modify":
                graph.append_notes(goal.id, f"HITL guidance: {decision.instruction}", by_session=session.id)
            if decision.action == "re_decompose":
                replaced = await self._re_decompose(
                    run, rt, goal, decision.instruction
                )
                if replaced:
                    persisted = await rt.store.load_graph(run.id)
                    if persisted is not None:
                        graph._nodes = dict(persisted._nodes)
                else:
                    goal.status = GoalStatus.PENDING
                    goal.assigned_to_session = None
                    goal.version += 1
                    await rt.store.release_goal(run.id, goal.id)
            else:
                goal.status = GoalStatus.PENDING
                goal.assigned_to_session = None
                goal.version += 1
                await rt.store.release_goal(run.id, goal.id)
            # Continue the loop (do not mark done)
            return True, final_validated, None

        if all_continue:
            graph.mark_done(goal.id, by_session=session.id)
            goal.assigned_to_session = None
            await rt.store.release_goal(run.id, goal.id)
            append_task_board(run.workspace_path, event="completed", goal_id=goal.id,
                              session_id=session.id, agent_type=run.task.agent.type)
        else:
            if goal.attempts >= goal.max_attempts:
                graph.mark_failed(goal.id, by_session=session.id)
                goal.assigned_to_session = None
                await rt.store.release_goal(run.id, goal.id)
                append_task_board(run.workspace_path, event="failed", goal_id=goal.id,
                                  session_id=session.id, agent_type=run.task.agent.type)
            else:
                goal.status = GoalStatus.PENDING
                goal.assigned_to_session = None
                goal.version += 1
                await rt.store.release_goal(run.id, goal.id)

        return True, final_validated, None

    # ------------------------------------------------------------------
    # HITL re-decomposition
    # ------------------------------------------------------------------

    async def _re_decompose(
        self, run: Run, rt: Any, current_goal: GoalNode, instruction: str
    ) -> bool:
        """LLM-restructures pending/in-progress goals based on operator instruction."""
        import sys

        graph_path = run.workspace_path / "goals.json"
        graph = await rt.store.load_graph(run.id)
        if graph is None:
            if not graph_path.exists():
                return False
            graph = GoalGraph.load(graph_path)
            await rt.store.create_graph(run.id, graph)
        done_ids = {nid for nid, n in graph._nodes.items() if n.status.value == "done"}
        restructurable = {
            nid: n.model_dump(mode="json")
            for nid, n in graph._nodes.items()
            if n.status.value != "done"
        }
        if not restructurable:
            return False

        prompt = (
            f"You are restructuring a goal graph for a long-horizon agent task.\n\n"
            f"TASK: {run.task.name}\n{run.task.prompt[:500]}\n\n"
            f"OPERATOR INSTRUCTION: {instruction}\n\n"
            f"CURRENT PENDING/IN-PROGRESS GOALS (JSON):\n{json.dumps(restructurable, indent=2)}\n\n"
            f"DONE GOALS (preserve, do not include in response): {json.dumps(list(done_ids))}\n\n"
            "Produce a revised set of pending goals as JSON: "
            '{\"nodes\": {\"g.root\": {...}, \"g.sub1\": {\"parent_id\": \"g.root\", ...}}}\n'
            "Rules: all IDs start with 'g.', every non-root node has parent_id, no cycles, "
            "leaf goals completable in one 25-min session."
        )

        for attempt in range(2):
            try:
                result = await call_llm_json(
                    system="You are a task decomposition expert. Return only valid JSON.",
                    user_prompt=prompt,
                    model="claude-haiku-4-5",
                )
                new_nodes_raw = result.get("nodes", {})
                if not new_nodes_raw:
                    continue

                merged_nodes: dict[str, GoalNode] = {}
                for nid, node in graph._nodes.items():
                    if node.status.value == "done":
                        merged_nodes[nid] = node

                for nid, raw in new_nodes_raw.items():
                    raw.setdefault("status", "pending")
                    raw.setdefault("attempts", 0)
                    raw.setdefault("notes", "")
                    raw.setdefault("children", [])
                    raw.setdefault("verification_criteria", [])
                    raw.setdefault("description", raw.get("name", ""))
                    node_data = {k: v for k, v in raw.items() if k != "id"}
                    merged_nodes[nid] = GoalNode(id=nid, **node_data)

                # Re-attach DONE nodes to their parents so the graph stays connected
                for nid, node in merged_nodes.items():
                    if node.status.value == "done" and node.parent_id:
                        parent = merged_nodes.get(node.parent_id)
                        if parent is not None and nid not in parent.children:
                            parent.children.append(nid)

                new_graph = GoalGraph(merged_nodes)
                await rt.store.replace_pending_subgraph(run.id, new_graph)
                await rt.store.ensure_goal_projection(run.id, graph_path)
                await rt.bus.publish(Event(
                    type="goals.re_decomposed",
                    run_id=run.id,
                    payload={"instruction": instruction, "new_goal_count": len(new_nodes_raw)},
                ))
                return True
            except Exception as exc:
                sys.stderr.write(f"[re_decompose] attempt {attempt + 1} failed: {exc}\n")
        return False
