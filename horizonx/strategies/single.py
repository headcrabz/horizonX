"""SingleSession strategy — one agent invocation runs to completion.

For tasks <30 steps. No goal graph, no checkpoints. See §21.1.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from horizonx.agents.base import Workspace
from horizonx.core.event_bus import Event
from horizonx.core.types import Run, RunStatus, SessionStatus, StrategyOutcome
from horizonx.strategies._agent_builder import build_agent as _build_agent


class SingleSession:
    kind = "single"

    def __init__(self, config: dict[str, Any]):
        self.config = config

    async def execute(self, run: Run, rt: Any) -> AsyncIterator[Event | StrategyOutcome]:
        session = await rt.start_session(run, target_goal=None)
        agent = _build_agent(run.task.agent)

        async def on_step(step: Any) -> None:
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

        rt.charge(result)
        await rt.end_session(session, result.status or SessionStatus.COMPLETED)
        if result.status != SessionStatus.COMPLETED:
            terminal_status = (
                RunStatus.TIMED_OUT
                if result.status == SessionStatus.TIMEOUT
                else RunStatus.FAILED
            )
            yield StrategyOutcome(
                status=terminal_status,
                reason=f"agent_{result.status.value}",
                details={"error": result.error},
            )
            return
        yield StrategyOutcome(status=RunStatus.COMPLETED)
