"""Stall-timeout watchdog.

Runs as an asyncio background task alongside an agent session.

  - At soft_seconds of silence: fires on_nudge callback (injects a
    continuation prompt into the agent) and records a soft-nudge event.
  - At hard_seconds of silence: cancels the session task and returns
    StallOutcome.HARD_ABORT so the runtime can pause for HITL.

The watchdog is non-intrusive when the agent is active: every call to
notify_activity() resets the idle timer and clears the nudge flag so a
second nudge is not sent until silence resumes.

Typical usage inside a strategy:

    watchdog = StallWatchdog(
        soft_seconds=task.resources.stall_soft_seconds,
        hard_seconds=task.resources.stall_hard_seconds,
    )

    async def _nudge(reason: str) -> None:
        # write a synthetic step / stdin nudge to the agent
        await runtime.bus.publish(Event(type="session.stall_nudge", ...))

    session_task = asyncio.create_task(agent.run_session(..., on_step=on_step_cb))
    outcome = await watchdog.run(session_task, on_nudge=_nudge)
    result  = await session_task   # already done; just collect result
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum


class StallOutcome(str, Enum):
    OK = "ok"
    SOFT_NUDGE = "soft_nudged"
    HARD_ABORT = "hard_aborted"


@dataclass
class StallWatchdog:
    """Idle-time monitor for a single agent session task."""

    soft_seconds: float = 120.0
    hard_seconds: float = 300.0
    poll_interval: float = 15.0

    _last_activity: float = field(default_factory=time.monotonic, init=False)
    _nudge_sent: bool = field(default=False, init=False)

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def notify_activity(self) -> None:
        """Call this on every new Step emitted by the agent."""
        self._last_activity = time.monotonic()
        self._nudge_sent = False

    async def run(
        self,
        session_task: "asyncio.Task[object]",
        *,
        on_nudge: Callable[[str], Awaitable[None]] | None = None,
    ) -> StallOutcome:
        """Monitor *session_task* until it finishes or a stall is detected.

        Returns the worst outcome that occurred:
          OK            — task finished within soft_seconds of activity
          SOFT_NUDGE    — at least one nudge was sent but task finished OK
          HARD_ABORT    — task was cancelled due to hard_seconds silence
        """
        outcome = StallOutcome.OK

        while not session_task.done():
            await asyncio.sleep(self.poll_interval)
            idle = time.monotonic() - self._last_activity

            if idle >= self.hard_seconds:
                session_task.cancel()
                outcome = StallOutcome.HARD_ABORT
                # Wait for cancellation to propagate before returning.
                try:
                    await session_task
                except (asyncio.CancelledError, Exception):
                    pass
                return outcome

            if idle >= self.soft_seconds and not self._nudge_sent:
                self._nudge_sent = True
                outcome = StallOutcome.SOFT_NUDGE
                if on_nudge is not None:
                    try:
                        await on_nudge(
                            f"No agent output for {idle:.0f}s. "
                            "Please report your current progress or ask for help."
                        )
                    except Exception:
                        pass  # nudge failure must never kill the watchdog

        return outcome
