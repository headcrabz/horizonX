"""Template for third-party HorizonX agent drivers.

Copy this file, implement run_session(), and register via pyproject.toml:

    [project.entry-points."horizonx.agents"]
    my_agent = "my_package.agents:MyAgent"

Then use in task YAML:
    agent:
      type: my_agent
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from horizonx.agents.base import CancelToken, Workspace
from horizonx.core.types import SessionRunResult, Step


class TemplateAgent:
    """Minimal agent driver — replace with your implementation."""

    name = "template"

    def __init__(self, config: Any) -> None:
        self.config = config

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
        # Your agent logic here.
        # - Call your LLM or SDK
        # - For each action, create a Step and call: await on_step(step)
        # - Check cancel_token.cancelled periodically and stop if True
        # - Return SessionRunResult with status=COMPLETED or ERRORED
        raise NotImplementedError("Implement run_session() in your agent driver")
