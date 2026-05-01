"""Tests for horizonx.strategies.tree."""

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


class TestTreeOfTrials:
    def test_init_defaults(self):
        from horizonx.strategies.tree import TreeOfTrials
        t = TreeOfTrials({})
        assert t.width == 3
        assert t.max_depth == 2
        assert t.accept_threshold == 0.85

    def test_init_custom(self):
        from horizonx.strategies.tree import TreeOfTrials
        t = TreeOfTrials({"width": 2, "max_depth": 3, "accept_threshold": 0.9})
        assert t.width == 2
        assert t.max_depth == 3

    @pytest.mark.asyncio
    async def test_execute_emits_events(self, tmp_path: Path):
        from horizonx.strategies.tree import TreeOfTrials

        tree = TreeOfTrials({"width": 2, "max_depth": 1, "scorer_type": "shell",
                              "scorer_command": "echo 0.9"})
        run = _make_run(tmp_path)
        rt = _make_mock_rt(tmp_path)

        with patch.object(tree, "_run_branch", new=AsyncMock()):
            with patch.object(tree, "_score_branch", new=AsyncMock(return_value=0.9)):
                events = []
                async for ev in tree.execute(run, rt):
                    events.append(ev)

        types = [e.type for e in events]
        assert "run.started" in types
        assert "run.completed" in types

    @pytest.mark.asyncio
    async def test_shell_score_parses_number(self, tmp_path: Path):
        from horizonx.strategies.tree import TreeOfTrials
        tree = TreeOfTrials({"scorer_command": "echo 0.75"})
        score = await tree._shell_score(tmp_path)
        assert abs(score - 0.75) < 0.01

    @pytest.mark.asyncio
    async def test_shell_score_exit_nonzero(self, tmp_path: Path):
        from horizonx.strategies.tree import TreeOfTrials
        tree = TreeOfTrials({"scorer_command": "exit 1"})
        score = await tree._shell_score(tmp_path)
        assert score == 0.0
