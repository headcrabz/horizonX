"""Event bus — every state change emits an Event.

In-memory pub/sub by default; swap in Redis/NATS for multi-process.
See docs/LONG_HORIZON_AGENT.md §18.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from horizonx.core.types import utcnow

EventType = Literal[
    "run.started",
    "run.completed",
    "run.failed",
    "run.paused_hitl",
    "session.started",
    "session.completed",
    "session.timeout",
    "step.recorded",
    "goal.in_progress",
    "goal.done",
    "goal.failed",
    "validator.passed",
    "validator.failed",
    "validator.paused",
    "spin.detected",
    "hitl.requested",
    "hitl.resolved",
    "budget.threshold",
    "summary.created",
    "fork.created",
    "fork.merged",
    "retry.attempted",
]


class Event(BaseModel):
    type: EventType
    run_id: str | None = None
    session_id: str | None = None
    timestamp: datetime = Field(default_factory=utcnow)
    payload: dict[str, Any] = Field(default_factory=dict)


class EventBus(Protocol):
    async def publish(self, event: Event) -> None: ...
    def subscribe(
        self, predicate: Callable[[Event], bool] | None = None
    ) -> AsyncIterator[Event]: ...


class InMemoryBus:
    """Single-process in-memory bus. One queue per subscriber."""

    def __init__(self) -> None:
        self._subscribers: list[tuple[asyncio.Queue[Event], Callable[[Event], bool] | None]] = []
        self._lock = asyncio.Lock()

    async def publish(self, event: Event) -> None:
        async with self._lock:
            for queue, pred in self._subscribers:
                if pred is None or pred(event):
                    await queue.put(event)

    async def subscribe(
        self, predicate: Callable[[Event], bool] | None = None
    ) -> AsyncIterator[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue()
        async with self._lock:
            self._subscribers.append((queue, predicate))
        try:
            while True:
                yield await queue.get()
        finally:
            async with self._lock:
                self._subscribers = [s for s in self._subscribers if s[0] is not queue]

    async def with_handler(
        self, handler: Callable[[Event], Awaitable[None]]
    ) -> None:
        """Run a handler against every event until cancelled."""
        async for event in self.subscribe():
            await handler(event)
