"""Tests for horizonx.strategies.pair."""

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


class TestPairProgramming:
    def test_init_defaults(self):
        from horizonx.strategies.pair import PairProgramming
        p = PairProgramming({})
        assert p.max_rounds == 4
        assert p.accept_threshold == 0.85

    def test_parse_score_from_guidance(self):
        from horizonx.strategies.pair import _parse_score_from_guidance
        md = "## Score\n0.92\n\n## Verdict\naccept"
        assert abs(_parse_score_from_guidance(md) - 0.92) < 0.001

    def test_parse_score_missing(self):
        from horizonx.strategies.pair import _parse_score_from_guidance
        assert _parse_score_from_guidance("no score here") == 0.5

    def test_parse_verdict(self):
        from horizonx.strategies.pair import _parse_verdict_from_guidance
        md = "## Verdict\naccept\n"
        assert _parse_verdict_from_guidance(md) == "accept"

    def test_parse_verdict_revise(self):
        from horizonx.strategies.pair import _parse_verdict_from_guidance
        md = "## Verdict\nrevise"
        assert _parse_verdict_from_guidance(md) == "revise"

    @pytest.mark.asyncio
    async def test_accepts_on_high_score(self, tmp_path: Path):
        from horizonx.strategies.pair import PairProgramming

        p = PairProgramming({"max_rounds": 3, "accept_threshold": 0.85})
        run = _make_run(tmp_path)
        rt = _make_mock_rt(tmp_path)

        call_count = [0]

        async def mock_run_session(prompt, workspace, **kwargs):
            from horizonx.core.types import SessionRunResult, SessionStatus
            call_count[0] += 1
            if call_count[0] % 2 == 0:  # navigator turn
                (tmp_path / "guidance.md").write_text(
                    "## Score\n0.90\n## Verdict\naccept\n"
                )
            return SessionRunResult(status=SessionStatus.COMPLETED)

        with patch("horizonx.strategies.pair._build_agent") as mock_build:
            mock_agent = MagicMock()
            mock_agent.run_session = mock_run_session
            mock_build.return_value = mock_agent

            events = []
            async for ev in p.execute(run, rt):
                events.append(ev)

        completed = [e for e in events if e.type == "run.completed"]
        assert len(completed) == 1
        assert completed[0].payload["rounds"] == 1
