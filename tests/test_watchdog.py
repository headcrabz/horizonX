"""Tests for horizonx.runtime.watchdog."""

from __future__ import annotations

import asyncio

import pytest

from horizonx.runtime.watchdog import StallOutcome, StallWatchdog


class TestStallWatchdog:
    def _fast_watchdog(self, soft: float = 0.05, hard: float = 0.15) -> StallWatchdog:
        return StallWatchdog(soft_seconds=soft, hard_seconds=hard, poll_interval=0.01)

    @pytest.mark.asyncio
    async def test_ok_when_task_finishes_quickly(self):
        watchdog = self._fast_watchdog()

        async def quick_task() -> str:
            return "done"

        task = asyncio.create_task(quick_task())
        watchdog.notify_activity()
        outcome = await watchdog.run(task)
        assert outcome == StallOutcome.OK

    @pytest.mark.asyncio
    async def test_soft_nudge_fires_before_hard(self):
        watchdog = self._fast_watchdog(soft=0.04, hard=0.20)
        nudge_calls: list[str] = []

        async def slow_task():
            await asyncio.sleep(0.10)

        async def on_nudge(reason: str) -> None:
            nudge_calls.append(reason)

        task = asyncio.create_task(slow_task())
        outcome = await watchdog.run(task, on_nudge=on_nudge)
        assert outcome == StallOutcome.SOFT_NUDGE
        assert len(nudge_calls) == 1

    @pytest.mark.asyncio
    async def test_hard_abort_cancels_task(self):
        watchdog = self._fast_watchdog(soft=0.02, hard=0.04)

        async def forever():
            await asyncio.sleep(9999)

        task = asyncio.create_task(forever())
        outcome = await watchdog.run(task)
        assert outcome == StallOutcome.HARD_ABORT
        assert task.done()

    @pytest.mark.asyncio
    async def test_notify_activity_resets_idle_timer(self):
        watchdog = self._fast_watchdog(soft=0.05, hard=0.15)
        nudge_calls: list[str] = []

        async def active_task():
            await asyncio.sleep(0.03)
            watchdog.notify_activity()
            await asyncio.sleep(0.03)

        async def on_nudge(reason: str) -> None:
            nudge_calls.append(reason)

        task = asyncio.create_task(active_task())
        outcome = await watchdog.run(task, on_nudge=on_nudge)
        assert outcome == StallOutcome.OK
        assert len(nudge_calls) == 0

    @pytest.mark.asyncio
    async def test_nudge_failure_does_not_crash_watchdog(self):
        watchdog = self._fast_watchdog(soft=0.02, hard=0.20)

        async def slow_task():
            await asyncio.sleep(0.10)

        async def bad_nudge(reason: str) -> None:
            raise RuntimeError("nudge exploded")

        task = asyncio.create_task(slow_task())
        outcome = await watchdog.run(task, on_nudge=bad_nudge)
        assert outcome == StallOutcome.SOFT_NUDGE
