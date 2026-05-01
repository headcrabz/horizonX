"""Tests for horizonx.strategies.decomposition."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_run(tmp_path: Path) -> Any:
    from horizonx.core.types import AgentConfig, ResourceLimits, Run, StrategyConfig, Task
    task = Task(
        id="t1",
        name="Test task",
        description="desc",
        prompt="Write a hello world program",
        strategy=StrategyConfig(kind="single"),
        agent=AgentConfig(type="mock", model="claude-haiku-4-5"),
        resources=ResourceLimits(max_total_hours=1),
    )
    return Run(id="run-test", task=task, workspace_path=tmp_path, status="pending")


_session_seq = 0


async def _make_session(*args: Any, **kwargs: Any) -> Any:
    global _session_seq
    _session_seq += 1
    from horizonx.core.types import Session
    return Session(id=f"sess-{_session_seq:04d}", run_id="run-test", sequence_index=_session_seq)


def _make_mock_rt(tmp_path: Path) -> Any:
    rt = MagicMock()
    rt.start_session = AsyncMock(side_effect=_make_session)
    rt.end_session = AsyncMock()
    rt.record_step = AsyncMock()
    rt.run_validators = AsyncMock(return_value=[])
    rt.request_hitl = AsyncMock()
    return rt


class TestDecompositionFirst:
    def test_init_defaults(self):
        from horizonx.strategies.decomposition import DecompositionFirst
        d = DecompositionFirst({})
        assert d.decomposer_model == "claude-haiku-4-5"
        assert d.max_attempts_per_goal == 3

    @pytest.mark.asyncio
    async def test_decompose_creates_goal_graph(self, tmp_path: Path):
        from horizonx.strategies.decomposition import DecompositionFirst

        d = DecompositionFirst({})
        run = _make_run(tmp_path)

        mock_subgoals = {
            "subgoals": [
                {"name": "Step 1", "description": "Do step 1", "verification_criteria": ["passes"]},
                {"name": "Step 2", "description": "Do step 2", "verification_criteria": ["passes"]},
            ]
        }
        with patch("horizonx.core.llm_client.call_llm_json", new=AsyncMock(return_value=mock_subgoals)):
            graph = await d._decompose(run)

        nodes = list(graph.all_nodes())
        assert len(nodes) == 3
        leaves = graph.leaves()
        assert len(leaves) == 2

    @pytest.mark.asyncio
    async def test_decompose_fallback_on_llm_error(self, tmp_path: Path):
        from horizonx.strategies.decomposition import DecompositionFirst

        d = DecompositionFirst({})
        run = _make_run(tmp_path)

        with patch("horizonx.core.llm_client.call_llm_json", new=AsyncMock(side_effect=Exception("err"))):
            graph = await d._decompose(run)

        nodes = list(graph.all_nodes())
        assert len(nodes) == 1

    @pytest.mark.asyncio
    async def test_execute_uses_existing_goals_json(self, tmp_path: Path):
        from horizonx.core.goal_graph import GoalGraph
        from horizonx.strategies.decomposition import DecompositionFirst

        graph = GoalGraph.empty("Root", "desc")
        graph.mark_in_progress("g.root", by_session="s1")
        graph.mark_done("g.root", by_session="s1")
        graph.save(tmp_path / "goals.json")

        d = DecompositionFirst({})
        run = _make_run(tmp_path)
        rt = _make_mock_rt(tmp_path)

        events = []
        async for ev in d.execute(run, rt):
            events.append(ev)

        types = [e.type for e in events]
        assert "run.completed" in types
