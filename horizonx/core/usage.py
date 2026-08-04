"""Per-workspace daily token/cost accounting."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


class UsageStore:
    """Records token and cost consumption per workspace per day.

    Backed by the same SqliteStore as the rest of HorizonX — uses a new
    workspace_usage table added to the schema.
    """

    def __init__(self, store: Any) -> None:
        self._store = store

    async def record(self, workspace_id: str, run_id: str,
                     tokens_in: int, tokens_out: int, usd: float | None) -> None:
        await self._store.record_workspace_usage(workspace_id, run_id, tokens_in, tokens_out, usd)

    async def daily_usd(self, workspace_id: str) -> float | None:
        value = await self._store.workspace_daily_usd(workspace_id)
        return float(value) if value is not None else None

    async def over_budget(self, workspace_id: str, daily_budget_usd: float) -> bool:
        spent = await self.daily_usd(workspace_id)
        return spent is not None and spent >= daily_budget_usd


@dataclass
class CostVelocityMonitor:
    """Sliding-window cost velocity detector.

    Fires when USD/minute rate doubles twice in succession — signature of a
    runaway loop rather than steady execution.
    """
    window: int = 5
    threshold_usd_per_min: float = 1.0
    _samples: deque[tuple[float, float]] = field(default_factory=lambda: deque(maxlen=5))
    _prev_slope: float = 0.0
    _doublings: int = 0

    def record(self, usd: float) -> None:
        self._samples.append((time.monotonic(), usd))

    def is_runaway(self) -> bool:
        if len(self._samples) < 3:
            return False
        times = [s[0] for s in self._samples]
        costs = [s[1] for s in self._samples]
        elapsed = times[-1] - times[0]
        if elapsed <= 0:
            return False
        rate_per_min = sum(costs) / (elapsed / 60)
        if rate_per_min < self.threshold_usd_per_min:
            self._doublings = 0
            return False
        if self._prev_slope > 0 and rate_per_min >= self._prev_slope * 2:
            self._doublings += 1
        else:
            self._doublings = 0
        self._prev_slope = rate_per_min
        return self._doublings >= 2
