"""PairProgramming — Driver/Navigator two-agent collaboration.

Two agents take alternating roles:
  Driver:    writes/edits code; focuses on implementation details
  Navigator: reviews Driver's output, spots issues, proposes next steps

Each round:
  1. Driver session: implements based on Navigator guidance (or task prompt on round 0)
  2. Navigator session: reviews the current workspace state, scores it,
     and writes guidance.md with instructions for the next Driver round
  3. Repeat until score >= accept_threshold or max_rounds exhausted

The key insight vs SelfCritique: Navigator is also an agent (with full tool
access), not just an LLM judge — it can actually run tests, check types,
measure coverage, read logs, etc., and produce richer, grounded guidance.

See docs/LONG_HORIZON_AGENT.md §21.8.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from horizonx.core.attempt_executor import AttemptExecutor
from horizonx.core.event_bus import Event
from horizonx.core.types import Run, RunStatus, StrategyOutcome

NAVIGATOR_SYSTEM_TEMPLATE = """\
You are the Navigator in a pair-programming loop. Review the current state of the
workspace and provide structured guidance for the next Driver session.

Your output must be written to `guidance.md` in the workspace root.

Structure guidance.md as:
# Navigator Review — Round {round_n}

## Score
<float 0.0-1.0 assessing how close the workspace is to the goal>

## Verdict
accept | revise

## Critical Issues (must fix)
- ...

## Suggestions
- ...

## Next Actions for Driver
1. ...
2. ...

Be concrete and actionable. Reference specific files and line numbers where possible.
Score 0.85+ only if the implementation genuinely satisfies the goal below.

GOAL:
{goal}
"""

DRIVER_TEMPLATE = """\
This is Driver round {round_n} of {max_rounds}.

Read `guidance.md` in the workspace for the Navigator's review of your previous work.
Address ALL critical issues listed there before making any other improvements.

Original goal:
{goal}
"""



def _parse_score_from_guidance(guidance_md: str) -> float:
    import re
    # Look for "## Score\n<float>" pattern
    match = re.search(r"##\s*Score\s*\n\s*([\d.]+)", guidance_md, re.IGNORECASE)
    if match:
        try:
            return max(0.0, min(1.0, float(match.group(1))))
        except ValueError:
            pass
    return 0.5


def _parse_verdict_from_guidance(guidance_md: str) -> str:
    import re
    match = re.search(r"##\s*Verdict\s*\n\s*(\w+)", guidance_md, re.IGNORECASE)
    if match:
        return match.group(1).lower()
    return "revise"


class PairProgramming:
    kind = "pair"

    def __init__(self, config: dict[str, Any]):
        self.max_rounds: int = config.get("max_rounds", 4)
        self.accept_threshold: float = config.get("accept_threshold", 0.85)
        # Navigator can be the same agent type or a lighter model
        self.navigator_model: str | None = config.get("navigator_model")

    async def execute(self, run: Run, rt: Any) -> AsyncIterator[Event | StrategyOutcome]:
        yield Event(type="run.started", run_id=run.id, payload={"strategy": "pair"})

        nav_config = run.task.agent.model_copy()
        if self.navigator_model:
            nav_config = run.task.agent.model_copy(update={"model": self.navigator_model})
        history: list[dict[str, Any]] = []

        for round_n in range(self.max_rounds):
            # ---- Driver session ----
            if round_n == 0:
                driver_prompt = run.task.prompt
            else:
                driver_prompt = DRIVER_TEMPLATE.format(
                    round_n=round_n + 1,
                    max_rounds=self.max_rounds,
                    goal=run.task.prompt,
                )

            driver_attempt = await AttemptExecutor(rt).execute(
                run, prompt=driver_prompt
            )
            driver_result = driver_attempt.agent

            if not driver_attempt.succeeded:
                yield StrategyOutcome(
                    status=RunStatus.FAILED,
                    reason="driver_failed",
                    details={"round": round_n, "error": driver_result.error},
                )
                return

            # ---- Navigator session ----
            nav_prompt = NAVIGATOR_SYSTEM_TEMPLATE.format(
                round_n=round_n + 1,
                goal=run.task.prompt[:800],
            )

            nav_attempt = await AttemptExecutor(rt).execute(
                run,
                prompt=nav_prompt,
                agent_config=nav_config,
            )
            nav_result = nav_attempt.agent
            if not nav_attempt.succeeded:
                yield StrategyOutcome(
                    status=RunStatus.FAILED,
                    reason="navigator_failed",
                    details={"round": round_n, "error": nav_result.error},
                )
                return

            # Read guidance.md written by navigator
            guidance_path = run.workspace_path / "guidance.md"
            guidance_text = guidance_path.read_text() if guidance_path.exists() else ""
            score = _parse_score_from_guidance(guidance_text)
            verdict = _parse_verdict_from_guidance(guidance_text)
            history.append({"round": round_n, "score": score, "verdict": verdict})

            yield Event(type="step.recorded", run_id=run.id, payload={
                "strategy": "pair", "round": round_n, "score": score, "verdict": verdict,
            })

            if verdict == "accept" or score >= self.accept_threshold:
                yield StrategyOutcome(
                    status=RunStatus.COMPLETED,
                    details={"rounds": round_n + 1, "final_score": score},
                )
                return

        final_score = history[-1]["score"] if history else 0.0
        yield StrategyOutcome(
            status=RunStatus.FAILED,
            reason="review_threshold_not_met",
            details={"rounds": self.max_rounds, "final_score": final_score},
        )
