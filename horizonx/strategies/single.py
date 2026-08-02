"""SingleSession strategy — one agent invocation runs to completion.

For tasks <30 steps. No goal graph, no checkpoints. See §21.1.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from horizonx.core.attempt_executor import AttemptExecutor
from horizonx.core.event_bus import Event
from horizonx.core.types import Run, RunStatus, SessionStatus, StrategyOutcome


class SingleSession:
    kind = "single"

    def __init__(self, config: dict[str, Any]):
        self.config = config

    async def execute(self, run: Run, rt: Any) -> AsyncIterator[Event | StrategyOutcome]:
        attempt = await AttemptExecutor(rt).execute(
            run,
            prompt=run.task.prompt,
        )
        if not attempt.succeeded:
            terminal_status = (
                RunStatus.TIMED_OUT
                if attempt.status == SessionStatus.TIMEOUT
                else RunStatus.FAILED
            )
            yield StrategyOutcome(
                status=terminal_status,
                reason=f"agent_{attempt.status.value}",
                details={"error": attempt.agent.error},
            )
            return
        yield StrategyOutcome(status=RunStatus.COMPLETED)
