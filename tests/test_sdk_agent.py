"""Tests for HX-12: SDKAgent."""
import pytest
from unittest.mock import AsyncMock
from pathlib import Path
from horizonx.agents.sdk import SDKAgent
from horizonx.core.types import SessionStatus, StepType, Step


def _make_config(callable_fn=None):
    class Config:
        type = "sdk"
        extra = {"callable": callable_fn} if callable_fn else {}
    return Config()


@pytest.mark.asyncio
async def test_sdk_agent_calls_on_step(tmp_path):
    async def my_gen(prompt, workspace_path):
        yield Step(session_id="", sequence=0, type=StepType.THOUGHT, content={"text": "thinking"})
        yield Step(session_id="", sequence=1, type=StepType.THOUGHT, content={"text": "done"})

    agent = SDKAgent(_make_config(my_gen))

    class FakeWorkspace:
        path = tmp_path

    steps_received = []
    async def on_step(step):
        steps_received.append(step)

    result = await agent.run_session("do the thing", FakeWorkspace(), on_step=on_step, session_id="s1")
    assert result.status == SessionStatus.COMPLETED
    assert len(steps_received) == 2
    assert steps_received[0].session_id == "s1"


@pytest.mark.asyncio
async def test_sdk_agent_no_callable_errors():
    agent = SDKAgent(_make_config())

    class FakeWorkspace:
        path = Path("/tmp")

    result = await agent.run_session("prompt", FakeWorkspace())
    assert result.status == SessionStatus.ERRORED
    assert "callable" in result.error


@pytest.mark.asyncio
async def test_sdk_agent_exception_returns_errored(tmp_path):
    async def bad_gen(prompt, workspace_path):
        raise RuntimeError("agent exploded")
        yield  # make it a generator

    agent = SDKAgent(_make_config(bad_gen))

    class FakeWorkspace:
        path = tmp_path

    result = await agent.run_session("prompt", FakeWorkspace())
    assert result.status == SessionStatus.ERRORED
    assert "exploded" in result.error
