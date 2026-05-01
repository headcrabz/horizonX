"""ResourceGovernor — enforces hard budgets on tokens, cost, wall-clock.

See docs/LONG_HORIZON_AGENT.md §19.
"""

from __future__ import annotations

import time
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING

from horizonx.core.event_bus import Event, EventBus
from horizonx.core.types import ResourceLimits, Run

if TYPE_CHECKING:
    pass


class BudgetExceeded(Exception):
    pass


class ResourceGovernor(AbstractAsyncContextManager):
    """Tracks consumed resources; raises BudgetExceeded when limits hit."""

    def __init__(self, limits: ResourceLimits, run: Run, bus: EventBus):
        self.limits = limits
        self.run = run
        self.bus = bus
        self._start_at = 0.0
        self._notified: set[int] = set()

    async def __aenter__(self) -> "ResourceGovernor":
        self._start_at = time.monotonic()
        return self

    async def __aexit__(self, *exc):  # type: ignore[no-untyped-def]
        return None

    def charge(self, *, tokens_in: int = 0, tokens_out: int = 0, usd: float = 0.0) -> None:
        c = self.run.cumulative
        c.tokens_in += tokens_in
        c.tokens_out += tokens_out
        c.usd += usd
        c.wall_seconds = time.monotonic() - self._start_at
        self._check_thresholds()

    def _check_thresholds(self) -> None:
        for pct in (50, 75, 90):
            if pct in self._notified:
                continue
            if self._utilization() >= pct / 100.0:
                self._notified.add(pct)
                # Fire-and-forget notification; bus is async but we're sync here
                import asyncio

                asyncio.create_task(
                    self.bus.publish(
                        Event(
                            type="budget.threshold",
                            run_id=self.run.id,
                            payload={"pct": pct, "cumulative": self.run.cumulative.model_dump()},
                        )
                    )
                )
        if self._utilization() >= 1.0:
            raise BudgetExceeded(
                f"resource limit reached: {self.run.cumulative.model_dump()}"
            )

    def _utilization(self) -> float:
        c = self.run.cumulative
        utils = []
        if self.limits.max_total_tokens:
            utils.append((c.tokens_in + c.tokens_out) / self.limits.max_total_tokens)
        if self.limits.max_total_usd:
            utils.append(c.usd / self.limits.max_total_usd)
        if self.limits.max_total_hours:
            utils.append(c.wall_seconds / (self.limits.max_total_hours * 3600))
        return max(utils) if utils else 0.0
