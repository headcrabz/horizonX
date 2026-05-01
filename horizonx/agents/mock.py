"""MockAgent — deterministic agent driver for testing.

Emits a configurable sequence of Steps without calling any real CLI.
Supports simulating: normal completion, errors, spin patterns, file changes,
tool calls, and cancellation.

Usage in tests:
    agent = MockAgent(steps=[
        {"type": "thought", "content": {"text": "Planning..."}},
        {"type": "tool_call", "tool_name": "Bash", "content": {"command": "echo hi"}},
        {"type": "observation", "tool_name": "Bash", "content": {"output": "hi"}},
    ])
    result = await agent.run_session("do something", workspace)

Usage in task YAML (for integration tests):
    agent:
      type: mock
      model: mock
      extra:
        steps: [...]
        status: completed  # or errored, timeout, spin
        delay_per_step: 0.01
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from horizonx.agents.base import CancelToken, Workspace
from horizonx.core.types import (
    AgentConfig,
    SessionRunResult,
    SessionStatus,
    Step,
    StepType,
)


class MockAgent:
    name = "mock"

    def __init__(
        self,
        steps: list[dict[str, Any]] | None = None,
        status: SessionStatus = SessionStatus.COMPLETED,
        error: str | None = None,
        delay_per_step: float = 0.0,
        agent_session_id: str = "mock-session-001",
        config: AgentConfig | None = None,
    ):
        self._steps = steps or [
            {"type": "thought", "content": {"text": "Mock agent starting..."}},
            {"type": "tool_call", "tool_name": "Bash", "content": {"command": "echo done"}},
            {"type": "observation", "tool_name": "Bash", "content": {"output": "done"}},
        ]
        self._status = status
        self._error = error
        self._delay = delay_per_step
        self._agent_session_id = agent_session_id
        if config and isinstance(config, AgentConfig):
            self._steps = config.extra.get("steps", self._steps)
            self._status = SessionStatus(config.extra.get("status", "completed"))
            self._error = config.extra.get("error")
            self._delay = config.extra.get("delay_per_step", 0.0)

    async def run_session(
        self,
        session_prompt: str,
        workspace: Workspace,
        *,
        resume_session_id: str | None = None,
        on_step: Callable[[Step], Awaitable[None]] | None = None,
        cancel_token: CancelToken | None = None,
        session_id: str | None = None,
    ) -> SessionRunResult:
        sid = session_id or "mock-session"
        for i, step_spec in enumerate(self._steps):
            if cancel_token and cancel_token.cancelled:
                return SessionRunResult(
                    agent_session_id=self._agent_session_id,
                    status=SessionStatus.TIMEOUT,
                    error=cancel_token.reason,
                )

            if self._delay > 0:
                await asyncio.sleep(self._delay)

            step = Step(
                session_id=sid,
                sequence=i,
                type=StepType(step_spec.get("type", "thought")),
                tool_name=step_spec.get("tool_name"),
                content=step_spec.get("content", {}),
            )
            if on_step:
                await on_step(step)

        return SessionRunResult(
            agent_session_id=self._agent_session_id,
            status=self._status,
            error=self._error,
        )
