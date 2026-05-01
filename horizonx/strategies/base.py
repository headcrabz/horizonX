"""Strategy protocol — every execution pattern implements this.

See docs/LONG_HORIZON_AGENT.md §21–§23.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, TYPE_CHECKING

from horizonx.core.event_bus import Event

if TYPE_CHECKING:
    from horizonx.core.runtime import Runtime
    from horizonx.core.types import Run


class Strategy(Protocol):
    """A Strategy decides which sub-goals to attempt and how to retry."""

    kind: str

    def __init__(self, config: dict[str, Any]): ...

    async def execute(self, run: "Run", rt: "Runtime") -> AsyncIterator[Event]:
        """Drive the run to completion or failure. Yields events for the bus."""
        ...
