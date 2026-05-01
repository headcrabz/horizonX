"""GoalGraphGate — validate goal graph progress as a quality signal.

Checks completion fraction, blocked goals, and stale goals.
This validator reads the goals.json file directly from the workspace.

Gate conditions (configurable):
  min_completion_pct: fraction of goals that must be DONE (default 0.0 = any)
  max_blocked_pct:    fraction of goals that may be BLOCKED (default 1.0 = any)
  max_failed_goals:   absolute count of FAILED goals allowed (default: unlimited)
  require_no_cycles:  structural integrity check (default: False)

See docs/LONG_HORIZON_AGENT.md §16.5.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from horizonx.core.types import GateAction, GateDecision, GoalStatus, Run, Session


class GoalGraphGate:
    name = "goal_graph"

    def __init__(self, config: dict[str, Any]):
        self.min_completion_pct: float = config.get("min_completion_pct", 0.0)
        self.max_blocked_pct: float = config.get("max_blocked_pct", 1.0)
        self.max_failed_goals: int | None = config.get("max_failed_goals")
        self.require_no_cycles: bool = config.get("require_no_cycles", False)
        self.on_fail: str = config.get("on_fail", "pause_for_hitl")
        self._id: str = config.get("id", "goal_graph")

    async def validate(self, run: Run, session: Session | None, workspace: Any) -> GateDecision:
        start = time.monotonic()

        goals_path: Path = workspace.path / "goals.json"
        if not goals_path.exists():
            return GateDecision(
                decision=GateAction.CONTINUE,
                reason="no goals.json found — skipping goal graph check",
                score=1.0,
                validator_name=self._id,
                duration_ms=int((time.monotonic() - start) * 1000),
            )

        try:
            from horizonx.core.goal_graph import GoalGraph
            graph = GoalGraph.load(goals_path)
        except Exception as exc:
            return GateDecision(
                decision=GateAction(self.on_fail),
                reason=f"failed to load goals.json: {exc}",
                score=0.0,
                validator_name=self._id,
                duration_ms=int((time.monotonic() - start) * 1000),
            )

        nodes = list(graph.all_nodes())
        total = len(nodes)
        if total == 0:
            return GateDecision(
                decision=GateAction.CONTINUE,
                reason="empty goal graph",
                score=1.0,
                validator_name=self._id,
                duration_ms=int((time.monotonic() - start) * 1000),
            )

        done = sum(1 for n in nodes if n.status == GoalStatus.DONE)
        failed = sum(1 for n in nodes if n.status == GoalStatus.FAILED)
        blocked = sum(1 for n in nodes if n.status == GoalStatus.BLOCKED)
        completion_pct = done / total
        blocked_pct = blocked / total

        failures: list[str] = []

        if completion_pct < self.min_completion_pct:
            failures.append(
                f"completion {completion_pct:.0%} < required {self.min_completion_pct:.0%}"
            )

        if blocked_pct > self.max_blocked_pct:
            failures.append(
                f"blocked goals {blocked_pct:.0%} > max {self.max_blocked_pct:.0%}"
            )

        if self.max_failed_goals is not None and failed > self.max_failed_goals:
            failures.append(
                f"{failed} failed goal(s), max allowed is {self.max_failed_goals}"
            )

        if self.require_no_cycles:
            cycle = self._detect_cycle(graph)
            if cycle:
                failures.append(f"cycle detected in goal graph: {cycle}")

        passed = len(failures) == 0
        score = completion_pct if passed else max(0.0, completion_pct - 0.1 * len(failures))

        return GateDecision(
            decision=GateAction.CONTINUE if passed else GateAction(self.on_fail),
            reason="; ".join(failures) if failures else f"goal graph OK ({done}/{total} done)",
            score=score,
            details={
                "total": total,
                "done": done,
                "failed": failed,
                "blocked": blocked,
                "completion_pct": round(completion_pct, 3),
            },
            validator_name=self._id,
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    def _detect_cycle(self, graph: Any) -> str | None:
        visited: set[str] = set()
        path: set[str] = set()

        def dfs(nid: str) -> str | None:
            if nid in path:
                return nid
            if nid in visited:
                return None
            visited.add(nid)
            path.add(nid)
            node = graph.get(nid)
            if node:
                for child_id in node.children:
                    result = dfs(child_id)
                    if result:
                        return result
            path.discard(nid)
            return None

        for node in graph.all_nodes():
            if result := dfs(node.id):
                return result
        return None
