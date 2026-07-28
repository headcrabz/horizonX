"""DecompositionFirst — LLM-driven task decomposition before execution.

Phase 1 (Decomposer): a cheap LLM call breaks the top-level prompt into an
ordered list of sub-goals written to goals.json.
Phase 2 (Executor): runs each sub-goal in sequence via agent sessions,
just like SequentialSubgoals but with LLM-generated goals instead of
agent-generated ones.

The key difference from SequentialSubgoals is that decomposition happens
upfront via a direct LLM call (no agent session), so it's fast, cheap,
and produces a structured plan before any expensive agent work begins.

See docs/LONG_HORIZON_AGENT.md §21.4.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from horizonx.agents.base import CancelToken, Workspace
from horizonx.core.event_bus import Event
from horizonx.core.goal_graph import GoalGraph
from horizonx.core.task_board import append_task_board
from horizonx.core.types import GoalStatus, Run, Step
from horizonx.strategies._agent_builder import build_agent as _build_agent

DECOMPOSER_SYSTEM = """\
You are a task planner for a long-horizon agent framework. Given a high-level goal,
decompose it into an ordered list of concrete, verifiable sub-goals.

Rules:
- Each sub-goal must be independently executable by a coding agent
- Each sub-goal must have clear, binary verification criteria
- Order sub-goals so each builds on the previous
- Use 3-8 sub-goals (more if genuinely needed, never artificial splits)
- Sub-goals must together fully accomplish the top-level goal

Output ONLY a JSON object:
{
  "subgoals": [
    {
      "name": "<short imperative name>",
      "description": "<1-2 sentence description>",
      "verification_criteria": ["<criterion 1>", "<criterion 2>"]
    }
  ]
}
"""



class DecompositionFirst:
    kind = "decomposition"

    def __init__(self, config: dict[str, Any]):
        self.decomposer_model: str = config.get("decomposer_model", "claude-haiku-4-5")
        self.max_attempts_per_goal: int = config.get("max_attempts_per_goal", 3)
        self.max_subgoals: int = config.get("max_subgoals", 12)

    async def execute(self, run: Run, rt: Any) -> AsyncIterator[Event]:
        graph_path = run.workspace_path / "goals.json"

        if not graph_path.exists():
            yield Event(type="run.started", run_id=run.id, payload={"phase": "decompose"})
            graph = await self._decompose(run)
            graph.save(graph_path)
            yield Event(type="goal.in_progress", run_id=run.id, payload={
                "phase": "decomposed",
                "subgoal_count": len(list(graph.all_nodes())),
            })
        else:
            graph = GoalGraph.load(graph_path)

        yield Event(type="run.started", run_id=run.id, payload={"phase": "execute"})

        while True:
            goal = graph.next_pending_leaf()
            if goal is None:
                break

            # Atomically claim in DB — safe for future parallel execution
            claimed = await rt.store.claim_goal(run.id, goal.id, session_id="pending")
            if not claimed:
                continue  # another agent claimed it; pick next leaf

            graph.mark_in_progress(goal.id, by_session="pending")
            graph.save(graph_path)

            session = await rt.start_session(run, target_goal=goal)
            graph.mark_in_progress(goal.id, by_session=session.id)
            goal.assigned_to_session = session.id
            graph.save(graph_path)
            append_task_board(
                run.workspace_path,
                event="claimed",
                goal_id=goal.id,
                session_id=session.id,
                agent_type=run.task.agent.type,
            )

            agent = _build_agent(run.task.agent)
            workspace = Workspace(path=run.workspace_path, env={})
            cancel = CancelToken()

            prompt = (
                f"Sub-goal: {goal.name}\n\n"
                f"Description: {goal.description}\n\n"
                f"Verification criteria:\n"
                + "\n".join(f"- {c}" for c in goal.verification_criteria)
                + f"\n\nOriginal task context:\n{run.task.prompt[:1000]}"
            )

            async def on_step(step: Step, s: Any = session) -> None:
                step.session_id = s.id
                await rt.record_step(s, step)

            result = await agent.run_session(
                prompt, workspace, on_step=on_step,
                cancel_token=cancel, session_id=session.id,
            )
            if result.agent_session_id:
                session.agent_session_id = result.agent_session_id

            decisions = await rt.run_validators(run, session, when="after_every_session")

            from horizonx.core.types import GateAction
            any_abort = any(d.decision == GateAction.ABORT for d in decisions)
            all_continue = all(d.decision == GateAction.CONTINUE for d in decisions) if decisions else True

            if any_abort or (not all_continue and goal.attempts >= self.max_attempts_per_goal):
                graph.mark_failed(goal.id, by_session=session.id)
                await rt.store.release_goal(run.id, goal.id)
                append_task_board(run.workspace_path, event="failed", goal_id=goal.id,
                                  session_id=session.id, agent_type=run.task.agent.type)
            elif all_continue:
                graph.mark_done(goal.id, by_session=session.id)
                await rt.store.release_goal(run.id, goal.id)
                append_task_board(run.workspace_path, event="completed", goal_id=goal.id,
                                  session_id=session.id, agent_type=run.task.agent.type)
            else:
                goal.attempts += 1

            graph.save(graph_path)

            event_type = "goal.done" if goal.status == GoalStatus.DONE else "goal.in_progress"
            yield Event(type=event_type,  # type: ignore[arg-type]
            run_id=run.id, payload={
                "goal_id": goal.id, "status": goal.status.value,
            })

            if graph.is_complete():
                break

        await rt.run_validators(run, None, when="final")
        if graph.is_complete():
            yield Event(type="run.completed", run_id=run.id, payload={"strategy": "decomposition"})
        else:
            yield Event(type="run.failed", run_id=run.id, payload={"reason": "subgoals_incomplete"})

    async def _decompose(self, run: Run) -> GoalGraph:
        from horizonx.core.llm_client import call_llm_json

        try:
            result = await call_llm_json(
                system=DECOMPOSER_SYSTEM,
                user_prompt=f"TASK:\n{run.task.prompt}",
                model=self.decomposer_model,
                max_tokens=2048,
                cache_system=True,
            )
            subgoals = result.get("subgoals", [])[:self.max_subgoals]
        except Exception:
            subgoals = []

        graph = GoalGraph.empty(run.task.name, run.task.description or run.task.prompt[:200])
        root = graph.root

        for i, sg in enumerate(subgoals):
            from horizonx.core.types import GoalNode
            gid = f"g.sg{i + 1:02d}"
            node_deps = [f"g.sg{i:02d}"] if i > 0 else []
            child = GoalNode(
                id=gid,
                name=sg.get("name", f"Sub-goal {i + 1}"),
                description=sg.get("description", ""),
                verification_criteria=sg.get("verification_criteria", []),
                depends_on=node_deps,
            )
            graph.add_child(root.id, child)

        return graph
