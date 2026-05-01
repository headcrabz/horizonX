"""SingleSession strategy — one agent invocation runs to completion.

For tasks <30 steps. No goal graph, no checkpoints. See §21.1.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from horizonx.agents.base import Workspace
from horizonx.agents.claude_code import ClaudeCodeAgent
from horizonx.agents.codex import CodexAgent
from horizonx.core.event_bus import Event
from horizonx.core.types import Run, SessionStatus


def _build_agent(ac: Any):
    if ac.type == "claude_code":
        return ClaudeCodeAgent(ac)
    if ac.type == "codex":
        return CodexAgent(ac)
    if ac.type == "custom":
        from horizonx.agents.custom import CustomAgent
        return CustomAgent(ac)
    if ac.type == "mock":
        from horizonx.agents.mock import MockAgent
        return MockAgent(config=ac)
    raise ValueError(f"unknown agent type for SingleSession: {ac.type}")


class SingleSession:
    kind = "single"

    def __init__(self, config: dict[str, Any]):
        self.config = config

    async def execute(self, run: Run, rt: Any) -> AsyncIterator[Event]:
        session = await rt.start_session(run, target_goal=None)
        agent = _build_agent(run.task.agent)

        async def on_step(step):
            step.session_id = session.id
            await rt.record_step(session, step)

        workspace = Workspace(path=run.workspace_path, env={})
        result = await agent.run_session(
            session_prompt=run.task.prompt,
            workspace=workspace,
            on_step=on_step,
            session_id=session.id,
        )
        if result.agent_session_id:
            session.agent_session_id = result.agent_session_id

        await rt.run_validators(run, session, when="final")
        await rt.end_session(session, result.status or SessionStatus.COMPLETED)
        if result.status in {SessionStatus.ERRORED, SessionStatus.SPIN, SessionStatus.TIMEOUT}:
            yield Event(
                type="run.failed",
                run_id=run.id,
                payload={"strategy": "single", "session_status": result.status.value, "error": result.error},
            )
            return
        yield Event(type="run.completed", run_id=run.id, payload={"strategy": "single"})
