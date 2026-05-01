"""Tests for horizonx.validators.goal_graph."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


class TestGoalGraphGate:
    def test_init_defaults(self):
        from horizonx.validators.goal_graph import GoalGraphGate
        g = GoalGraphGate({})
        assert g.min_completion_pct == 0.0
        assert g.max_failed_goals is None

    @pytest.mark.asyncio
    async def test_passes_without_goals_json(self, tmp_path: Path):
        from horizonx.validators.goal_graph import GoalGraphGate

        g = GoalGraphGate({})
        workspace = MagicMock()
        workspace.path = tmp_path
        run = MagicMock()

        decision = await g.validate(run, None, workspace)
        assert decision.decision.value == "continue"
        assert "no goals.json" in decision.reason

    @pytest.mark.asyncio
    async def test_passes_all_done(self, tmp_path: Path):
        from horizonx.core.goal_graph import GoalGraph
        from horizonx.validators.goal_graph import GoalGraphGate

        graph = GoalGraph.empty("Root", "desc")
        graph.mark_in_progress("g.root", by_session="s1")
        graph.mark_done("g.root", by_session="s1")
        graph.save(tmp_path / "goals.json")

        g = GoalGraphGate({"min_completion_pct": 1.0})
        workspace = MagicMock()
        workspace.path = tmp_path
        run = MagicMock()

        decision = await g.validate(run, None, workspace)
        assert decision.decision.value == "continue"
        assert decision.score == 1.0

    @pytest.mark.asyncio
    async def test_fails_completion_below_threshold(self, tmp_path: Path):
        from horizonx.core.goal_graph import GoalGraph
        from horizonx.core.types import GoalNode
        from horizonx.validators.goal_graph import GoalGraphGate

        graph = GoalGraph.empty("Root", "desc")
        child = GoalNode(id="g.c1", name="Child", description="d")
        graph.add_child("g.root", child)
        graph.save(tmp_path / "goals.json")

        g = GoalGraphGate({"min_completion_pct": 1.0, "on_fail": "pause_for_hitl"})
        workspace = MagicMock()
        workspace.path = tmp_path
        run = MagicMock()

        decision = await g.validate(run, None, workspace)
        assert decision.decision.value == "pause_for_hitl"
        assert "completion" in decision.reason

    @pytest.mark.asyncio
    async def test_fails_on_too_many_failed_goals(self, tmp_path: Path):
        from horizonx.core.goal_graph import GoalGraph
        from horizonx.core.types import GoalNode
        from horizonx.validators.goal_graph import GoalGraphGate

        graph = GoalGraph.empty("Root", "desc")
        child = GoalNode(id="g.c1", name="Child", description="d")
        graph.add_child("g.root", child)
        graph.mark_in_progress("g.c1", by_session="s1")
        graph.mark_failed("g.c1", by_session="s1")
        graph.save(tmp_path / "goals.json")

        g = GoalGraphGate({"max_failed_goals": 0, "on_fail": "pause_for_hitl"})
        workspace = MagicMock()
        workspace.path = tmp_path
        run = MagicMock()

        decision = await g.validate(run, None, workspace)
        assert decision.decision.value == "pause_for_hitl"
        assert "failed" in decision.reason

    def test_cycle_detection_no_cycle(self, tmp_path: Path):
        from horizonx.core.goal_graph import GoalGraph
        from horizonx.core.types import GoalNode
        from horizonx.validators.goal_graph import GoalGraphGate

        graph = GoalGraph.empty("Root", "desc")
        child = GoalNode(id="g.c1", name="Child", description="d")
        graph.add_child("g.root", child)

        g = GoalGraphGate({"require_no_cycles": True})
        result = g._detect_cycle(graph)
        assert result is None
