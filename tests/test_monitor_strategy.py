"""Tests for horizonx.strategies.monitor."""

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


class TestMonitorRespond:
    def test_init_defaults(self):
        from horizonx.strategies.monitor import MonitorRespond
        m = MonitorRespond({})
        assert m.poll_interval_seconds == 30.0
        assert m.max_triggers is None

    def test_threshold_ge(self):
        from horizonx.strategies.monitor import MonitorRespond
        m = MonitorRespond({"trigger_direction": "ge", "trigger_threshold": 5.0})
        assert m._threshold_met(5.0) is True
        assert m._threshold_met(4.9) is False

    def test_threshold_le(self):
        from horizonx.strategies.monitor import MonitorRespond
        m = MonitorRespond({"trigger_direction": "le", "trigger_threshold": 3.0})
        assert m._threshold_met(3.0) is True
        assert m._threshold_met(3.1) is False

    @pytest.mark.asyncio
    async def test_trigger_shell_exit0(self, tmp_path: Path):
        from horizonx.strategies.monitor import MonitorRespond
        m = MonitorRespond({"trigger_command": "true"})
        triggered = await m._check_trigger(tmp_path)
        assert triggered is True

    @pytest.mark.asyncio
    async def test_trigger_shell_exit1(self, tmp_path: Path):
        from horizonx.strategies.monitor import MonitorRespond
        m = MonitorRespond({"trigger_command": "false"})
        triggered = await m._check_trigger(tmp_path)
        assert triggered is False

    @pytest.mark.asyncio
    async def test_execute_fires_and_stops_at_max(self, tmp_path: Path):
        from horizonx.strategies.monitor import MonitorRespond

        m = MonitorRespond({
            "trigger_command": "true",
            "max_triggers": 2,
            "poll_interval_seconds": 0.01,
        })
        run = _make_run(tmp_path)
        rt = _make_mock_rt(tmp_path)

        with patch.object(m, "_run_responder", new=AsyncMock()):
            events = []
            async for ev in m.execute(run, rt):
                events.append(ev)

        completed = [e for e in events if e.type == "run.completed"]
        assert len(completed) == 1
        assert completed[0].payload["triggers_fired"] == 2
