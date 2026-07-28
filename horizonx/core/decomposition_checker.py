"""GE-01 — Decomposition quality pre-check.

Runs before the first session of any strategy that uses a goal graph.
Errors block the run. Warnings are recorded and surfaced in the dashboard
but do not block execution.

The discipline: a loop without well-defined goal nodes is an expensive
random walk. This checker makes bad decompositions immediately visible
instead of 12 sessions and $80 later.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from horizonx.core.goal_graph import GoalGraph
from horizonx.core.types import GoalNode, GoalStatus, Task

Severity = Literal["error", "warning", "info"]

_VAGUE_WORDS = frozenset({
    "improve", "update", "fix", "make", "better", "enhance", "refactor",
    "clean", "optimise", "optimize", "review", "check",
})

_MAX_CHILDREN = 8
_MIN_DESCRIPTION_CHARS = 20
_MIN_NAME_WORDS = 3


@dataclass
class DecompositionIssue:
    severity: Severity
    code: str          # e.g. "VAGUE_NAME", "NO_DESCRIPTION"
    goal_id: str | None
    message: str


@dataclass
class DecompositionReport:
    issues: list[DecompositionIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[DecompositionIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[DecompositionIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)

    @property
    def ok(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict[str, object]:
        return {
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "issues": [
                {
                    "severity": i.severity,
                    "code": i.code,
                    "goal_id": i.goal_id,
                    "message": i.message,
                }
                for i in self.issues
            ],
        }


class DecompositionChecker:
    """Check goal graph quality before the first agent session runs."""

    def check_graph(self, graph: GoalGraph, task: Task) -> DecompositionReport:
        report = DecompositionReport()
        nodes = list(graph.all_nodes())

        # Single-node graph — might indicate failed decomposition
        if len(nodes) == 1:
            report.issues.append(DecompositionIssue(
                severity="info",
                code="SINGLE_NODE",
                goal_id=nodes[0].id,
                message="Goal graph has only one node — consider decomposing into sub-goals for better tracking.",
            ))

        for node in nodes:
            self._check_node(node, report, task)

        return report

    def check_file(self, goals_path: Path, task: Task) -> DecompositionReport:
        """Load goals.json and check it. Returns empty report if file missing."""
        if not goals_path.exists():
            return DecompositionReport()
        graph = GoalGraph.load(goals_path)
        return self.check_graph(graph, task)

    # ------------------------------------------------------------------

    def _check_node(self, node: GoalNode, report: DecompositionReport, task: Task) -> None:
        self._check_vague_name(node, report)
        self._check_description(node, report)
        self._check_too_many_children(node, report)
        self._check_no_verification_criteria(node, report)
        self._check_failed_goal_with_no_attempts(node, report)

    def _check_vague_name(self, node: GoalNode, report: DecompositionReport) -> None:
        words = node.name.lower().split()
        if len(words) < _MIN_NAME_WORDS:
            report.issues.append(DecompositionIssue(
                severity="warning",
                code="SHORT_NAME",
                goal_id=node.id,
                message=(
                    f"'{node.name}' is very short ({len(words)} word(s)). "
                    "Goal names should be specific enough to be verifiable."
                ),
            ))
            return
        vague = _VAGUE_WORDS.intersection(words)
        if vague:
            report.issues.append(DecompositionIssue(
                severity="warning",
                code="VAGUE_NAME",
                goal_id=node.id,
                message=(
                    f"'{node.name}' contains vague word(s): {', '.join(sorted(vague))}. "
                    "Use specific, verifiable language (e.g. 'implement /token endpoint returning JWT' "
                    "rather than 'fix authentication')."
                ),
            ))

    def _check_description(self, node: GoalNode, report: DecompositionReport) -> None:
        if not node.description or len(node.description.strip()) < _MIN_DESCRIPTION_CHARS:
            report.issues.append(DecompositionIssue(
                severity="warning",
                code="NO_DESCRIPTION",
                goal_id=node.id,
                message=(
                    f"Goal '{node.id}' has no meaningful description "
                    f"(got {len(node.description.strip())} chars, need ≥{_MIN_DESCRIPTION_CHARS}). "
                    "Descriptions tell the agent what exactly needs to be true."
                ),
            ))

    def _check_too_many_children(self, node: GoalNode, report: DecompositionReport) -> None:
        if len(node.children) > _MAX_CHILDREN:
            report.issues.append(DecompositionIssue(
                severity="warning",
                code="TOO_MANY_CHILDREN",
                goal_id=node.id,
                message=(
                    f"Goal '{node.id}' has {len(node.children)} children "
                    f"(max recommended: {_MAX_CHILDREN}). "
                    "Consider grouping into intermediate sub-goals."
                ),
            ))

    def _check_no_verification_criteria(self, node: GoalNode, report: DecompositionReport) -> None:
        # Only flag leaf nodes — non-leaves are verified through their children
        if not node.children and not node.verification_criteria:
            report.issues.append(DecompositionIssue(
                severity="warning",
                code="NO_CRITERIA",
                goal_id=node.id,
                message=(
                    f"Leaf goal '{node.id}' has no verification criteria. "
                    "Without criteria, the agent has no definition of done."
                ),
            ))

    def _check_failed_goal_with_no_attempts(self, node: GoalNode, report: DecompositionReport) -> None:
        # A FAILED goal with 0 attempts indicates a data inconsistency
        if node.status == GoalStatus.FAILED and node.attempts == 0:
            report.issues.append(DecompositionIssue(
                severity="warning",
                code="FAILED_NO_ATTEMPTS",
                goal_id=node.id,
                message=(
                    f"Goal '{node.id}' is marked FAILED but shows 0 attempts — "
                    "may indicate a graph state inconsistency."
                ),
            ))
