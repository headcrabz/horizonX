"""Tests for CustomAgent — subprocess-backed agent driver."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from horizonx.agents.base import CancelToken, Workspace
from horizonx.agents.custom import CustomAgent
from horizonx.core.types import AgentConfig, SessionStatus, StepType


def _cfg(extra: dict) -> AgentConfig:
    return AgentConfig(type="custom", model="test-model", extra=extra)


def _ws(tmp_path: Path) -> Workspace:
    return Workspace(path=tmp_path, env={})


class TestCustomAgentInit:
    def test_string_command_parsed(self):
        agent = CustomAgent(_cfg({"command": "echo hello world"}))
        assert agent._cmd == ["echo", "hello", "world"]

    def test_list_command_kept(self):
        agent = CustomAgent(_cfg({"command": ["python", "run.py"]}))
        assert agent._cmd == ["python", "run.py"]

    def test_missing_command_raises(self):
        with pytest.raises(ValueError, match="extra.command"):
            CustomAgent(_cfg({}))

    def test_defaults(self):
        agent = CustomAgent(_cfg({"command": "echo hi"}))
        assert agent._prompt_mode == "stdin"
        assert agent._output_format == "text"
        assert agent._timeout == 1800.0
        assert agent._extra_args == []
        assert agent._extra_env == {}

    def test_custom_options(self):
        agent = CustomAgent(_cfg({
            "command": "my_agent",
            "prompt_mode": "arg",
            "output_format": "jsonl",
            "timeout": 60.0,
            "args": ["--verbose"],
            "env": {"FOO": "bar"},
        }))
        assert agent._prompt_mode == "arg"
        assert agent._output_format == "jsonl"
        assert agent._timeout == 60.0
        assert agent._extra_args == ["--verbose"]
        assert agent._extra_env == {"FOO": "bar"}


class TestCustomAgentRun:
    @pytest.mark.asyncio
    async def test_text_output_becomes_thought_steps(self, tmp_path: Path):
        steps: list = []

        async def collect(step):
            steps.append(step)

        agent = CustomAgent(_cfg({"command": "echo 'hello from agent'"}))
        ws = _ws(tmp_path)
        result = await agent.run_session("do something", ws, on_step=collect)
        assert result.status == SessionStatus.COMPLETED
        assert len(steps) == 1
        assert steps[0].type == StepType.THOUGHT
        assert "hello" in steps[0].content["text"]

    @pytest.mark.asyncio
    async def test_jsonl_output_parsed(self, tmp_path: Path):
        line = json.dumps({"type": "tool_call", "tool_name": "Bash", "content": {"command": "ls"}})
        cmd = f"printf '{line}\\n'"
        steps: list = []

        async def collect(step):
            steps.append(step)

        agent = CustomAgent(_cfg({"command": cmd, "output_format": "jsonl"}))
        ws = _ws(tmp_path)
        result = await agent.run_session("task", ws, on_step=collect)
        assert result.status == SessionStatus.COMPLETED
        assert len(steps) == 1
        assert steps[0].type == StepType.TOOL_CALL
        assert steps[0].tool_name == "Bash"

    @pytest.mark.asyncio
    async def test_binary_not_found(self, tmp_path: Path):
        agent = CustomAgent(_cfg({"command": "this-binary-definitely-does-not-exist-xyz"}))
        result = await agent.run_session("task", _ws(tmp_path))
        assert result.status == SessionStatus.ERRORED
        assert "not found" in (result.error or "")

    @pytest.mark.asyncio
    async def test_nonzero_exit_is_errored(self, tmp_path: Path):
        agent = CustomAgent(_cfg({"command": "false"}))
        result = await agent.run_session("task", _ws(tmp_path))
        assert result.status == SessionStatus.ERRORED
        assert "exit" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_prompt_passed_via_stdin(self, tmp_path: Path):
        # Write stdin to a file, then check the file
        out = tmp_path / "captured.txt"
        cmd = f"sh -c 'cat > {out}'"
        agent = CustomAgent(_cfg({"command": cmd, "prompt_mode": "stdin"}))
        await agent.run_session("my prompt", _ws(tmp_path))
        assert out.read_text() == "my prompt"

    @pytest.mark.asyncio
    async def test_prompt_passed_via_arg(self, tmp_path: Path):
        out = tmp_path / "captured.txt"
        cmd = f"sh -c 'echo \"$1\" > {out}' --"
        agent = CustomAgent(_cfg({"command": cmd, "prompt_mode": "arg"}))
        await agent.run_session("arg-prompt", _ws(tmp_path))
        assert "arg-prompt" in out.read_text()

    @pytest.mark.asyncio
    async def test_prompt_passed_via_env(self, tmp_path: Path):
        out = tmp_path / "captured.txt"
        cmd = f"sh -c 'echo \"$HORIZONX_PROMPT\" > {out}'"
        agent = CustomAgent(_cfg({"command": cmd, "prompt_mode": "env"}))
        await agent.run_session("env-prompt", _ws(tmp_path))
        assert "env-prompt" in out.read_text()

    @pytest.mark.asyncio
    async def test_prompt_passed_via_file(self, tmp_path: Path):
        out = tmp_path / "captured.txt"
        cmd = f"sh -c 'cat \"$HORIZONX_PROMPT_FILE\" > {out}'"
        agent = CustomAgent(_cfg({"command": cmd, "prompt_mode": "file"}))
        await agent.run_session("file-prompt", _ws(tmp_path))
        assert "file-prompt" in out.read_text()

    @pytest.mark.asyncio
    async def test_workspace_env_injected(self, tmp_path: Path):
        out = tmp_path / "ws_path.txt"
        cmd = f"sh -c 'echo \"$HORIZONX_WORKSPACE\" > {out}'"
        agent = CustomAgent(_cfg({"command": cmd}))
        await agent.run_session("task", _ws(tmp_path))
        assert str(tmp_path) in out.read_text()

    @pytest.mark.asyncio
    async def test_cancel_token_stops_agent(self, tmp_path: Path):
        import asyncio
        token = CancelToken()
        agent = CustomAgent(_cfg({"command": "sleep 60"}))
        task = asyncio.create_task(
            agent.run_session("task", _ws(tmp_path), cancel_token=token)
        )
        await asyncio.sleep(0.05)
        token.cancel("test cancel")
        result = await asyncio.wait_for(task, timeout=5.0)
        assert result.status == SessionStatus.TIMEOUT

    @pytest.mark.asyncio
    async def test_timeout_triggers(self, tmp_path: Path):
        agent = CustomAgent(_cfg({"command": "sleep 60", "timeout": 0.1}))
        result = await agent.run_session("task", _ws(tmp_path))
        assert result.status == SessionStatus.TIMEOUT
        assert "timeout" in (result.error or "").lower()
