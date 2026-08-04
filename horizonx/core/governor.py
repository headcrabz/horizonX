"""ResourceGovernor — enforces hard budgets on tokens, cost, wall-clock.

See docs/LONG_HORIZON_AGENT.md §19.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Any

from horizonx.core.event_bus import Event, EventBus
from horizonx.core.types import ResourceLimits, Run

if TYPE_CHECKING:
    pass


class BudgetExceeded(Exception):
    pass


class ResourceGovernor(AbstractAsyncContextManager["ResourceGovernor"]):
    """Tracks consumed resources; raises BudgetExceeded when limits hit."""

    def __init__(
        self,
        limits: ResourceLimits,
        run: Run,
        bus: EventBus,
        hitl_callback: Callable[..., Any] | None = None,
        usage_store: Any = None,
        velocity_monitor: Any = None,
    ):
        self.limits = limits
        self.run = run
        self.bus = bus
        self.hitl_callback = hitl_callback
        self.usage_store = usage_store
        self.velocity_monitor = velocity_monitor
        self._start_at = 0.0
        self._elapsed_before = 0.0
        self._notified: set[int] = set()

    async def __aenter__(self) -> ResourceGovernor:
        self._elapsed_before = self.run.cumulative.wall_seconds
        self._start_at = time.monotonic()
        return self

    async def __aexit__(self, *exc):  # type: ignore[no-untyped-def]
        return None

    def checkpoint_wall_time(self) -> None:
        """Update active elapsed time without charging usage."""
        self.run.cumulative.wall_seconds = (
            self._elapsed_before + time.monotonic() - self._start_at
        )

    async def charge(
        self,
        *,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
        usd: float | None = None,
    ) -> None:
        c = self.run.cumulative
        c.tokens_in += tokens_in
        c.tokens_out += tokens_out
        c.cache_creation_tokens += cache_creation_tokens
        c.cache_read_tokens += cache_read_tokens
        cache_eligible = c.tokens_in + c.cache_read_tokens
        c.cache_hit_rate = (
            c.cache_read_tokens / cache_eligible if cache_eligible else 0.0
        )
        if usd is None:
            c.usd = None
            c.cost_known = False
        elif c.cost_known:
            c.usd = (c.usd or 0.0) + usd
        self.checkpoint_wall_time()

        # Velocity monitoring
        if self.velocity_monitor is not None and usd is not None:
            self.velocity_monitor.record(usd)
            if self.velocity_monitor.is_runaway():
                await self.bus.publish(Event(
                    type="budget.velocity_alert",
                    run_id=self.run.id,
                    payload={"cumulative": self.run.cumulative.model_dump()},
                ))
                if self.hitl_callback is not None:
                    callback_result = self.hitl_callback(
                        self.run, reason="budget_velocity_runaway",
                        context={"cumulative": self.run.cumulative.model_dump()},
                    )
                    if inspect.isawaitable(callback_result):
                        await callback_result

        # Per-workspace daily accounting is durable before charge returns.
        if self.usage_store is not None and self.run.task.workspace is not None:
            workspace = self.run.task.workspace
            ws_id = workspace.workspace_id
            await self.usage_store.record(
                ws_id, self.run.id, tokens_in, tokens_out, usd,
            )
            if workspace.daily_budget_usd is not None:
                daily_spent = await self.usage_store.daily_usd(ws_id)
                if (
                    daily_spent is not None
                    and daily_spent >= workspace.daily_budget_usd
                ):
                    raise BudgetExceeded(
                        f"workspace {ws_id!r} daily budget reached: "
                        f"${daily_spent:.2f}/${workspace.daily_budget_usd:.2f}"
                    )

        await self._check_thresholds()

    async def _check_thresholds(self) -> None:
        for pct in (50, 75, 90):
            if pct in self._notified:
                continue
            if self._utilization() >= pct / 100.0:
                self._notified.add(pct)
                # At 75%, trigger HITL if configured
                if (
                    pct == 75
                    and self.hitl_callback is not None
                    and self.run.task.hitl is not None
                    and "budget_threshold_75" in self.run.task.hitl.triggers
                ):
                    callback_result = self.hitl_callback(
                        self.run,
                        reason="budget_threshold_75",
                        context={"pct": 75, "cumulative": self.run.cumulative.model_dump()},
                    )
                    if inspect.isawaitable(callback_result):
                        await callback_result

                await self.bus.publish(
                    Event(
                        type="budget.threshold",
                        run_id=self.run.id,
                        payload={"pct": pct, "cumulative": self.run.cumulative.model_dump()},
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
            utils.append(
                (
                    c.tokens_in
                    + c.tokens_out
                    + c.cache_creation_tokens
                    + c.cache_read_tokens
                )
                / self.limits.max_total_tokens
            )
        if self.limits.max_total_usd and c.usd is not None:
            utils.append(c.usd / self.limits.max_total_usd)
        if self.limits.max_total_hours:
            utils.append(c.wall_seconds / (self.limits.max_total_hours * 3600))
        return max(utils) if utils else 0.0
