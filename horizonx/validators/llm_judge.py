"""LLMJudgeGate — LLM-as-judge validator for progress quality.

Uses a cheap model (Haiku 4.5 by default) with a configurable rubric to score
agent progress after each session. The system prompt is prompt-cached so
repeated validations within a run share the prefix.

Gate decisions:
  - score >= threshold → CONTINUE
  - score < threshold → on_fail action (default: PAUSE_FOR_HITL)
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from horizonx.core.types import GateAction, GateDecision, Run, Session, Step, StepType

logger = logging.getLogger(__name__)

JUDGE_SYSTEM = """\
You are a progress validator for a long-horizon agent execution framework.
Your job is to assess whether an agent session made meaningful progress toward
its stated goal, or whether it is stuck, spinning, or producing low-quality work.

You will receive:
1. The goal description and verification criteria
2. The agent's recent trajectory (tool calls, observations, file changes)
3. A rubric to evaluate against

Score on a 0.0-1.0 scale:
  0.0 = no progress, actively regressing or spinning
  0.3 = minimal progress, mostly wasted effort
  0.5 = some progress but significant issues
  0.7 = solid progress with minor concerns
  0.9 = excellent progress, goal nearly/fully achieved
  1.0 = goal fully achieved and verified

Output ONLY a JSON object:
{
  "score": 0.0-1.0,
  "reason": "<2-3 sentence assessment>",
  "concerns": ["<concern 1>", ...],
  "evidence": ["<evidence of progress or lack thereof>", ...]
}

Be strict. Agents often appear productive (many tool calls, many file changes)
without making real progress. Look for:
- Tests passing that weren't before
- Files that implement actual logic (not just boilerplate)
- Error rates going down
- Concrete artifacts matching the goal
- Edit-revert patterns (oscillating changes = no progress)
"""


class LLMJudgeGate:
    name = "llm_judge"

    def __init__(self, config: dict[str, Any], *, store: Any = None):
        self.rubric: str = config.get(
            "rubric", "Is the agent making real, measurable progress on the stated goal?"
        )
        self.model: str = config.get("model", "claude-haiku-4-5")
        self.threshold: float = config.get("threshold", 0.7)
        self.on_fail: str = config.get("on_fail", "pause_for_hitl")
        self._id: str = config.get("id", "llm_judge")
        self.max_trajectory_steps: int = config.get("max_trajectory_steps", 100)
        self.store = store

    async def validate(
        self, run: Run, session: Session | None, workspace: Any
    ) -> GateDecision:
        start = time.monotonic()

        if session is None:
            return GateDecision(
                decision=GateAction.CONTINUE,
                reason="No session to evaluate",
                score=1.0,
                validator_name=self._id,
            )

        goal = await self._get_goal(run, session, workspace)
        trajectory = await self._get_trajectory(session, workspace)

        user_prompt = self._build_prompt(goal, trajectory)

        try:
            result = await self._call_llm(user_prompt)
        except Exception as exc:
            logger.warning("LLMJudge call failed: %s — defaulting to CONTINUE", exc)
            return GateDecision(
                decision=GateAction.CONTINUE,
                reason=f"LLM judge call failed ({exc}), defaulting to continue",
                score=None,
                details={"error": str(exc)},
                validator_name=self._id,
                duration_ms=int((time.monotonic() - start) * 1000),
            )

        score = float(result.get("score", 0.5))
        reason = result.get("reason", "No reason provided")
        concerns = result.get("concerns", [])
        evidence = result.get("evidence", [])

        if score >= self.threshold:
            decision = GateAction.CONTINUE
        else:
            decision = GateAction(self.on_fail)

        return GateDecision(
            decision=decision,
            reason=reason,
            score=score,
            details={
                "rubric": self.rubric,
                "concerns": concerns,
                "evidence": evidence,
                "threshold": self.threshold,
                "model": self.model,
            },
            validator_name=self._id,
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    def _build_prompt(self, goal: dict[str, Any], trajectory: str) -> str:
        return (
            f"RUBRIC: {self.rubric}\n\n"
            f"GOAL:\n"
            f"  Name: {goal.get('name', 'unknown')}\n"
            f"  Description: {goal.get('description', 'none')}\n"
            f"  Verification criteria: {json.dumps(goal.get('verification_criteria', []))}\n\n"
            f"TRAJECTORY:\n{trajectory}"
        )

    async def _get_goal(self, run: Run, session: Session, workspace: Any) -> dict[str, Any]:
        if not session.target_goal_id:
            return {
                "name": run.task.name,
                "description": run.task.description or run.task.prompt[:500],
                "verification_criteria": [],
            }
        try:
            from horizonx.core.goal_graph import GoalGraph

            goals_path = run.workspace_path / "goals.json"
            if goals_path.exists():
                graph = GoalGraph.load(goals_path)
                node = graph.get(session.target_goal_id)
                if node:
                    return {
                        "name": node.name,
                        "description": node.description,
                        "verification_criteria": node.verification_criteria,
                    }
        except Exception:
            pass
        return {
            "name": session.target_goal_id,
            "description": run.task.prompt[:500],
            "verification_criteria": [],
        }

    async def _get_trajectory(self, session: Session, workspace: Any) -> str:
        store = self.store
        if store is None:
            return "(trajectory not available — no store access)"

        steps: list[Step] = await store.recent_steps(session.id, self.max_trajectory_steps)
        lines: list[str] = []
        for s in steps:
            if s.type in (StepType.USAGE, StepType.SESSION_ID, StepType.SYSTEM):
                continue
            label = s.tool_name or s.type.value
            content = json.dumps(s.content, default=str)[:200]
            lines.append(f"[{s.sequence}] {label}: {content}")
        return "\n".join(lines) if lines else "(empty trajectory)"

    async def _call_llm(self, user_prompt: str) -> dict[str, Any]:
        from horizonx.core.llm_client import call_llm_json

        return await call_llm_json(
            system=JUDGE_SYSTEM,
            user_prompt=user_prompt,
            model=self.model,
            max_tokens=1024,
            cache_system=True,
        )
