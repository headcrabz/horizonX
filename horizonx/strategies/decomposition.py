"""DecompositionFirst — LLM-driven task decomposition before execution.

Phase 1 (Decomposer): a cheap LLM call breaks the top-level prompt into an
ordered list of sub-goals committed to the orchestration store.
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
from horizonx.core.types import (
    GoalStatus,
    Run,
    RunStatus,
    SessionStatus,
    Step,
    StrategyOutcome,
    new_session_id,
)
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

    async def execute(self, run: Run, rt: Any) -> AsyncIterator[Event | StrategyOutcome]:
        graph_path = run.workspace_path / "goals.json"

        if not graph_path.exists():
            yield Event(type="run.started", run_id=run.id, payload={"phase": "decompose"})
            graph = await self._decompose(run)
            await rt.store.create_graph(run.id, graph)
            await rt.store.ensure_goal_projection(run.id, graph_path)
            yield Event(type="goal.in_progress", run_id=run.id, payload={
                "phase": "decomposed",
                "subgoal_count": len(list(graph.all_nodes())),
            })
        else:
            graph = await rt.store.load_graph(run.id)
            if graph is None:
                graph = GoalGraph.load(graph_path)
                await rt.store.create_graph(run.id, graph)
            await rt.store.ensure_goal_projection(run.id, graph_path)

        yield Event(type="run.started", run_id=run.id, payload={"phase": "execute"})

        final_validated = False
        while True:
            goal = graph.next_pending_leaf()
            if goal is None:
                break

            # Atomically claim in DB — safe for future parallel execution
            session_id = new_session_id()
            claimed = await rt.store.claim_goal(run.id, goal.id, session_id=session_id)
            if not claimed:
                yield StrategyOutcome(
                    status=RunStatus.FAILED, reason="goal_claim_unavailable"
                )
                return

            graph.mark_in_progress(goal.id, by_session=session_id)
            goal.assigned_to_session = session_id
            await rt.store.create_graph(run.id, graph)
            await rt.store.ensure_goal_projection(run.id, graph_path)

            session = await rt.start_session(
                run, target_goal=goal, session_id=session_id
            )
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
            rt.charge(result)

            if result.status != SessionStatus.COMPLETED:
                graph.mark_failed(goal.id, by_session=session.id)
                goal.assigned_to_session = None
                await rt.store.release_goal(run.id, goal.id)
                await rt.store.create_graph(run.id, graph)
                await rt.store.ensure_goal_projection(run.id, graph_path)
                await rt.end_session(session, result.status)
                yield StrategyOutcome(
                    status=(
                        RunStatus.TIMED_OUT
                        if result.status == SessionStatus.TIMEOUT
                        else RunStatus.FAILED
                    ),
                    reason=f"agent_{result.status.value}",
                    details={"goal_id": goal.id, "error": result.error},
                )
                return

            decisions = await rt.run_validators(run, session, when="after_every_session")
            if all(
                leaf.id == goal.id or leaf.status == GoalStatus.DONE
                for leaf in graph.leaves()
            ):
                decisions.extend(await rt.run_validators(run, session, when="final"))
                final_validated = True

            from horizonx.core.types import GateAction
            any_abort = any(d.decision == GateAction.ABORT for d in decisions)
            any_pause = any(
                d.decision == GateAction.PAUSE_FOR_HITL for d in decisions
            )
            all_continue = all(d.decision == GateAction.CONTINUE for d in decisions) if decisions else True

            terminal_override: StrategyOutcome | None = None
            if any_abort:
                graph.mark_failed(goal.id, by_session=session.id)
                goal.assigned_to_session = None
                await rt.store.release_goal(run.id, goal.id)
                append_task_board(run.workspace_path, event="failed", goal_id=goal.id,
                                  session_id=session.id, agent_type=run.task.agent.type)
                terminal_override = StrategyOutcome(
                    status=RunStatus.ABORTED,
                    reason="validator_aborted",
                    details={"goal_id": goal.id},
                )
            elif any_pause:
                decision = await rt.request_hitl(
                    run,
                    reason="validator_pause",
                    context={
                        "goal_id": goal.id,
                        "validator_decisions": [d.model_dump() for d in decisions],
                    },
                )
                if decision.action == "abort":
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
                    terminal_override = StrategyOutcome(
                        status=RunStatus.ABORTED,
                        reason="operator_aborted",
                        details={"goal_id": goal.id},
                    )
                else:
                    if decision.action == "modify":
                        graph.append_notes(
                            goal.id,
                            f"HITL guidance: {decision.instruction}",
                            by_session=session.id,
                        )
                    goal.status = GoalStatus.PENDING
                    goal.assigned_to_session = None
                    goal.version += 1
                    await rt.store.release_goal(run.id, goal.id)
            elif all_continue:
                graph.mark_done(goal.id, by_session=session.id)
                goal.assigned_to_session = None
                await rt.store.release_goal(run.id, goal.id)
                append_task_board(run.workspace_path, event="completed", goal_id=goal.id,
                                  session_id=session.id, agent_type=run.task.agent.type)
            elif goal.attempts >= self.max_attempts_per_goal:
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

            await rt.end_session(session, result.status or SessionStatus.COMPLETED)
            await rt.store.create_graph(run.id, graph)
            await rt.store.ensure_goal_projection(run.id, graph_path)

            event_type = "goal.done" if goal.status == GoalStatus.DONE else "goal.in_progress"
            yield Event(type=event_type,  # type: ignore[arg-type]
            run_id=run.id, payload={
                "goal_id": goal.id, "status": goal.status.value,
            })

            if terminal_override is not None:
                yield terminal_override
                return

            if graph.is_complete():
                break

        if graph.is_complete():
            yield StrategyOutcome(
                status=RunStatus.COMPLETED,
                details={"_final_validated": final_validated},
            )
        else:
            yield StrategyOutcome(
                status=RunStatus.FAILED, reason="subgoals_incomplete"
            )

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
