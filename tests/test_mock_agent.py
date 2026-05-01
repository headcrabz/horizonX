"""Tests for MockAgent — deterministic test driver."""

from __future__ import annotations

from pathlib import Path

import pytest

from horizonx.agents.base import CancelToken, Workspace
from horizonx.agents.mock import MockAgent
from horizonx.core.types import AgentConfig, SessionStatus, Step, StepType


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    return Workspace(path=tmp_path, env={})


class TestMockAgent:
    @pytest.mark.asyncio
    async def test_default_steps(self, workspace: Workspace):
        agent = MockAgent()
        steps: list[Step] = []
        result = await agent.run_session(
            "test", workspace, on_step=lambda s: _collect(steps, s)
        )
        assert result.status == SessionStatus.COMPLETED
        assert len(steps) == 3
        assert steps[0].type == StepType.THOUGHT
        assert steps[1].type == StepType.TOOL_CALL
        assert steps[2].type == StepType.OBSERVATION

    @pytest.mark.asyncio
    async def test_custom_steps(self, workspace: Workspace):
        agent = MockAgent(
            steps=[
                {"type": "thought", "content": {"text": "thinking"}},
                {"type": "file_change", "tool_name": "file_change",
                 "content": {"changes": [{"path": "a.py", "kind": "add"}]}},
            ],
            status=SessionStatus.COMPLETED,
        )
        steps: list[Step] = []
        result = await agent.run_session(
            "test", workspace, on_step=lambda s: _collect(steps, s)
        )
        assert len(steps) == 2
        assert steps[1].type == StepType.FILE_CHANGE

    @pytest.mark.asyncio
    async def test_error_status(self, workspace: Workspace):
        agent = MockAgent(
            steps=[],
            status=SessionStatus.ERRORED,
            error="test error",
        )
        result = await agent.run_session("test", workspace)
        assert result.status == SessionStatus.ERRORED
        assert result.error == "test error"

    @pytest.mark.asyncio
    async def test_cancellation(self, workspace: Workspace):
        cancel = CancelToken()
        cancel.cancel("budget exceeded")
        agent = MockAgent(
            steps=[{"type": "thought", "content": {"text": "t"}}] * 10,
            delay_per_step=0.01,
        )
        result = await agent.run_session(
            "test", workspace, cancel_token=cancel
        )
        assert result.status == SessionStatus.TIMEOUT

    @pytest.mark.asyncio
    async def test_from_agent_config(self, workspace: Workspace):
        config = AgentConfig(
            type="mock",
            model="mock",
            extra={
                "steps": [{"type": "thought", "content": {"text": "config-driven"}}],
                "status": "completed",
            },
        )
        agent = MockAgent(config=config)
        steps: list[Step] = []
        result = await agent.run_session(
            "test", workspace, on_step=lambda s: _collect(steps, s)
        )
        assert len(steps) == 1
        assert steps[0].content["text"] == "config-driven"

    @pytest.mark.asyncio
    async def test_session_id_passed_through(self, workspace: Workspace):
        agent = MockAgent()
        steps: list[Step] = []
        await agent.run_session(
            "test", workspace, session_id="custom-id",
            on_step=lambda s: _collect(steps, s),
        )
        assert all(s.session_id == "custom-id" for s in steps)


async def _collect(lst: list, step: Step) -> None:
    lst.append(step)
