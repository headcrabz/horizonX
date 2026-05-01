"""Tests for agent driver event parsing — Claude Code, Codex, OpenHands.

These test the _event_to_steps logic without spawning real CLI processes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from horizonx.agents.claude_code import ClaudeCodeAgent, ClaudeCodeConfig
from horizonx.agents.codex import CodexAgent, CodexConfig
from horizonx.core.types import AgentConfig, StepType


class TestClaudeCodeEventParsing:
    def setup_method(self):
        self.agent = ClaudeCodeAgent(ClaudeCodeConfig())

    def test_system_init(self):
        event = {
            "type": "system",
            "subtype": "init",
            "session_id": "abc-123",
            "model": "claude-sonnet-4-6",
            "tools": ["Bash", "Read", "Edit"],
        }
        steps = self.agent._event_to_steps(event, 0, "s1")
        assert len(steps) == 2
        assert steps[0].type == StepType.SYSTEM
        assert steps[1].type == StepType.SESSION_ID
        assert steps[1].content["session_id"] == "abc-123"

    def test_assistant_text(self):
        event = {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "I'll help you with that."}],
            },
        }
        steps = self.agent._event_to_steps(event, 0, "s1")
        assert len(steps) == 1
        assert steps[0].type == StepType.THOUGHT
        assert "help" in steps[0].content["text"]

    def test_assistant_thinking(self):
        event = {
            "type": "assistant",
            "message": {
                "content": [{"type": "thinking", "thinking": "Let me analyze this..."}],
            },
        }
        steps = self.agent._event_to_steps(event, 0, "s1")
        assert len(steps) == 1
        assert steps[0].type == StepType.REASONING
        assert "analyze" in steps[0].content["text"]

    def test_assistant_tool_use(self):
        event = {
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "id": "toolu_123",
                    "name": "Bash",
                    "input": {"command": "ls -la"},
                }],
            },
        }
        steps = self.agent._event_to_steps(event, 0, "s1")
        assert len(steps) == 1
        assert steps[0].type == StepType.TOOL_CALL
        assert steps[0].tool_name == "Bash"
        assert steps[0].content["input"]["command"] == "ls -la"

    def test_assistant_todowrite(self):
        event = {
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "id": "toolu_456",
                    "name": "TodoWrite",
                    "input": {"todos": [{"task": "step 1", "status": "pending"}]},
                }],
            },
        }
        steps = self.agent._event_to_steps(event, 0, "s1")
        assert len(steps) == 1
        assert steps[0].type == StepType.TODO_LIST
        assert steps[0].tool_name == "TodoWrite"

    def test_user_tool_result(self):
        event = {
            "type": "user",
            "message": {
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "toolu_123",
                    "content": "file contents here",
                    "is_error": False,
                }],
            },
        }
        steps = self.agent._event_to_steps(event, 0, "s1")
        assert len(steps) == 1
        assert steps[0].type == StepType.OBSERVATION
        assert steps[0].content["output"] == "file contents here"

    def test_result_event(self):
        event = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "total_cost_usd": 0.05,
            "usage": {"input_tokens": 1000, "output_tokens": 500},
        }
        steps = self.agent._event_to_steps(event, 0, "s1")
        assert len(steps) == 1
        assert steps[0].type == StepType.USAGE
        assert steps[0].content["total_cost_usd"] == 0.05

    def test_result_error(self):
        event = {
            "type": "result",
            "subtype": "error",
            "is_error": True,
            "result": "context window exceeded",
        }
        steps = self.agent._event_to_steps(event, 0, "s1")
        assert len(steps) == 2
        assert steps[0].type == StepType.USAGE
        assert steps[1].type == StepType.ERROR

    def test_mixed_content_blocks(self):
        event = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "planning..."},
                    {"type": "text", "text": "I'll run this command."},
                    {"type": "tool_use", "id": "t1", "name": "Bash",
                     "input": {"command": "echo hi"}},
                ],
            },
        }
        steps = self.agent._event_to_steps(event, 0, "s1")
        assert len(steps) == 3
        assert steps[0].type == StepType.REASONING
        assert steps[1].type == StepType.THOUGHT
        assert steps[2].type == StepType.TOOL_CALL


class TestClaudeCodeUsageAccumulation:
    def test_result_usage(self):
        agent = ClaudeCodeAgent(ClaudeCodeConfig())
        agent._accumulate_usage({
            "type": "result",
            "total_cost_usd": 0.03,
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 200,
                "cache_creation_input_tokens": 500,
                "cache_read_input_tokens": 300,
            },
        })
        assert agent._totals["input_tokens"] == 1000
        assert agent._totals["output_tokens"] == 200
        assert agent._totals["cache_creation_input_tokens"] == 500
        assert agent._totals["cache_read_input_tokens"] == 300
        assert agent._totals["total_cost_usd"] == 0.03

    def test_assistant_usage(self):
        agent = ClaudeCodeAgent(ClaudeCodeConfig())
        agent._accumulate_usage({
            "type": "assistant",
            "message": {
                "usage": {"input_tokens": 500, "output_tokens": 100},
            },
        })
        assert agent._totals["input_tokens"] == 500


class TestCodexEventParsing:
    def setup_method(self):
        self.agent = CodexAgent(CodexConfig())

    def test_thread_started(self):
        event = {"type": "thread.started", "thread_id": "uuid-123"}
        steps = self.agent._event_to_steps(event, 0, "s1")
        assert len(steps) == 1
        assert steps[0].type == StepType.SESSION_ID
        assert steps[0].content["session_id"] == "uuid-123"

    def test_turn_started(self):
        event = {"type": "turn.started"}
        steps = self.agent._event_to_steps(event, 0, "s1")
        assert len(steps) == 1
        assert steps[0].type == StepType.SYSTEM

    def test_turn_completed(self):
        event = {
            "type": "turn.completed",
            "usage": {"input_tokens": 800, "output_tokens": 200},
        }
        steps = self.agent._event_to_steps(event, 0, "s1")
        assert len(steps) == 1
        assert steps[0].type == StepType.USAGE

    def test_turn_failed(self):
        event = {
            "type": "turn.failed",
            "error": {"message": "rate limited"},
        }
        steps = self.agent._event_to_steps(event, 0, "s1")
        assert len(steps) == 1
        assert steps[0].type == StepType.ERROR
        assert "rate limited" in steps[0].content["error"]

    def test_agent_message(self):
        event = {
            "type": "item.completed",
            "item": {"id": "i1", "type": "agent_message", "text": "Done."},
        }
        steps = self.agent._event_to_steps(event, 0, "s1")
        assert len(steps) == 1
        assert steps[0].type == StepType.THOUGHT
        assert steps[0].content["text"] == "Done."

    def test_reasoning(self):
        event = {
            "type": "item.completed",
            "item": {"id": "i2", "type": "reasoning", "text": "Let me think..."},
        }
        steps = self.agent._event_to_steps(event, 0, "s1")
        assert len(steps) == 1
        assert steps[0].type == StepType.REASONING

    def test_command_execution_started(self):
        event = {
            "type": "item.started",
            "item": {"id": "i3", "type": "command_execution", "command": "npm test"},
        }
        steps = self.agent._event_to_steps(event, 0, "s1")
        assert len(steps) == 1
        assert steps[0].type == StepType.TOOL_CALL
        assert steps[0].content["command"] == "npm test"

    def test_command_execution_completed(self):
        event = {
            "type": "item.completed",
            "item": {
                "id": "i3",
                "type": "command_execution",
                "command": "npm test",
                "aggregated_output": "All tests passed",
                "exit_code": 0,
                "status": "completed",
            },
        }
        steps = self.agent._event_to_steps(event, 0, "s1")
        assert len(steps) == 1
        assert steps[0].type == StepType.OBSERVATION
        assert steps[0].content["exit_code"] == 0

    def test_file_change(self):
        event = {
            "type": "item.completed",
            "item": {
                "id": "i4",
                "type": "file_change",
                "changes": [{"path": "src/app.py", "kind": "update"}],
                "status": "completed",
            },
        }
        steps = self.agent._event_to_steps(event, 0, "s1")
        assert len(steps) == 1
        assert steps[0].type == StepType.FILE_CHANGE

    def test_todo_list(self):
        event = {
            "type": "item.started",
            "item": {
                "id": "i5",
                "type": "todo_list",
                "items": [{"text": "step 1", "done": False}],
            },
        }
        steps = self.agent._event_to_steps(event, 0, "s1")
        assert len(steps) == 1
        assert steps[0].type == StepType.TODO_LIST

    def test_item_updated_skipped(self):
        event = {
            "type": "item.updated",
            "item": {"id": "i1", "type": "agent_message", "text": "partial..."},
        }
        steps = self.agent._event_to_steps(event, 0, "s1")
        assert len(steps) == 0

    def test_error_item(self):
        event = {
            "type": "item.completed",
            "item": {"id": "i6", "type": "error", "message": "something broke"},
        }
        steps = self.agent._event_to_steps(event, 0, "s1")
        assert len(steps) == 1
        assert steps[0].type == StepType.ERROR

    def test_mcp_tool_call(self):
        event = {
            "type": "item.started",
            "item": {"id": "i7", "type": "mcp_tool_call", "name": "web_search",
                     "arguments": {"query": "test"}},
        }
        steps = self.agent._event_to_steps(event, 0, "s1")
        assert len(steps) == 1
        assert steps[0].type == StepType.TOOL_CALL
        assert steps[0].tool_name == "mcp_tool_call"


class TestCodexUsageAccumulation:
    def test_turn_completed_usage(self):
        agent = CodexAgent(CodexConfig())
        agent._accumulate_usage({
            "type": "turn.completed",
            "usage": {
                "input_tokens": 1000,
                "cached_input_tokens": 500,
                "output_tokens": 200,
            },
        })
        assert agent._totals["input_tokens"] == 1000
        assert agent._totals["cached_input_tokens"] == 500
        assert agent._totals["output_tokens"] == 200

    def test_multiple_turns_accumulate(self):
        agent = CodexAgent(CodexConfig())
        for _ in range(3):
            agent._accumulate_usage({
                "type": "turn.completed",
                "usage": {"input_tokens": 100, "output_tokens": 50},
            })
        assert agent._totals["input_tokens"] == 300
        assert agent._totals["output_tokens"] == 150


class TestOpenHandsAgent:
    def test_init_defaults(self):
        from horizonx.agents.openhands import OpenHandsAgent
        config = AgentConfig(type="openhands", model="gpt-4o", extra={})
        agent = OpenHandsAgent(config)
        assert agent.mode == "cli"
        assert agent.cli_bin == "openhands"
        assert agent.agent_cls == "CodeActAgent"

    def test_init_custom(self):
        from horizonx.agents.openhands import OpenHandsAgent
        config = AgentConfig(type="openhands", model="gpt-4o", extra={
            "mode": "server",
            "server_url": "http://myserver:3000",
            "max_iterations": 50,
        })
        agent = OpenHandsAgent(config)
        assert agent.mode == "server"
        assert agent.server_url == "http://myserver:3000"
        assert agent.max_iterations == 50

    @pytest.mark.asyncio
    async def test_cli_binary_not_found(self, tmp_path: Path):
        from horizonx.agents.openhands import OpenHandsAgent
        from horizonx.agents.base import Workspace
        from horizonx.core.types import SessionStatus
        config = AgentConfig(type="openhands", model="any", extra={"cli_bin": "nonexistent-binary-xyz"})
        agent = OpenHandsAgent(config)
        workspace = Workspace(path=tmp_path, env={})
        result = await agent.run_session("test task", workspace)
        assert result.status == SessionStatus.ERRORED
        assert "not found" in (result.error or "")

    def test_parse_cli_line_json(self, tmp_path: Path):
        from horizonx.agents.openhands import OpenHandsAgent
        config = AgentConfig(type="openhands", model="any", extra={})
        agent = OpenHandsAgent(config)
        step = agent._parse_cli_line('{"type": "action", "action": "run"}', 0, "sess-1")
        assert step is not None
        assert step.type == StepType.TOOL_CALL

    def test_parse_cli_line_plain_text(self, tmp_path: Path):
        from horizonx.agents.openhands import OpenHandsAgent
        config = AgentConfig(type="openhands", model="any", extra={})
        agent = OpenHandsAgent(config)
        step = agent._parse_cli_line("Plain text output", 0, "sess-1")
        assert step is not None
        assert step.type == StepType.THOUGHT
        assert step.content["text"] == "Plain text output"
