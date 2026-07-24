"""SDK agent driver — wraps any Python async callable as a HorizonX agent.

Allows API-based agents (OpenAI Agents SDK, Google Vertex, custom Python
functions) to plug into HorizonX without building a subprocess shim.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from horizonx.agents.base import CancelToken, Workspace
from horizonx.core.types import SessionRunResult, SessionStatus, Step


class SDKAgent:
    """Wraps a Python async generator as a HorizonX agent.

    The callable receives (prompt: str, workspace_path: Path) and must yield
    Step objects. Example:

        async def my_agent(prompt, workspace_path):
            yield Step(session_id="", sequence=0, type=StepType.THOUGHT,
                       content={"text": "thinking..."})

        agent_config.extra["callable"] = my_agent
    """

    name = "sdk"

    def __init__(self, config: Any) -> None:
        self.config = config
        self._callable: Callable | None = config.extra.get("callable") if hasattr(config, "extra") else None

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
        if self._callable is None:
            return SessionRunResult(
                status=SessionStatus.ERRORED,
                error="SDKAgent requires config.extra['callable'] to be set",
            )

        try:
            async for step in self._callable(session_prompt, workspace.path):
                if cancel_token and cancel_token.cancelled:
                    break
                if session_id:
                    step = step.model_copy(update={"session_id": session_id})
                if on_step:
                    await on_step(step)
            return SessionRunResult(status=SessionStatus.COMPLETED)
        except Exception as exc:
            return SessionRunResult(status=SessionStatus.ERRORED, error=str(exc))
