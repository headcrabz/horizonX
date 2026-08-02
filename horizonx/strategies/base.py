"""Strategy protocol — every execution pattern implements this.

See docs/LONG_HORIZON_AGENT.md §21–§23.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Protocol

from horizonx.core.event_bus import Event

if TYPE_CHECKING:
    from horizonx.core.runtime import Runtime
    from horizonx.core.types import Run, StrategyOutcome


class Strategy(Protocol):
    """A Strategy decides which sub-goals to attempt and how to retry."""

    kind: str

    def __init__(self, config: dict[str, Any]): ...

    async def execute(
        self, run: Run, rt: Runtime
    ) -> AsyncIterator[Event | StrategyOutcome]:
        """Yield progress events followed by exactly one terminal outcome."""
        ...
