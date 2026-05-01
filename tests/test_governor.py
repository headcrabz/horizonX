"""Tests for ResourceGovernor — budget enforcement."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from horizonx.core.event_bus import InMemoryBus
from horizonx.core.governor import BudgetExceeded, ResourceGovernor
from horizonx.core.types import (
    AgentConfig,
    ResourceLimits,
    Run,
    StrategyConfig,
    Task,
)


def _make_run(limits: ResourceLimits | None = None) -> Run:
    task = Task(
        id="t1",
        name="test",
        prompt="test",
        strategy=StrategyConfig(kind="single"),
        agent=AgentConfig(type="mock", model="mock"),
        resources=limits or ResourceLimits(),
    )
    return Run(task=task, workspace_path=Path("/tmp/test"))


class TestGovernor:
    @pytest.mark.asyncio
    async def test_normal_usage(self):
        run = _make_run(ResourceLimits(max_total_tokens=1_000_000))
        bus = InMemoryBus()
        gov = ResourceGovernor(run.task.resources, run, bus)
        async with gov:
            gov.charge(tokens_in=100, tokens_out=50)
            assert run.cumulative.tokens_in == 100
            assert run.cumulative.tokens_out == 50

    @pytest.mark.asyncio
    async def test_budget_exceeded(self):
        run = _make_run(ResourceLimits(max_total_tokens=100))
        bus = InMemoryBus()
        gov = ResourceGovernor(run.task.resources, run, bus)
        async with gov:
            with pytest.raises(BudgetExceeded):
                gov.charge(tokens_in=60, tokens_out=60)

    @pytest.mark.asyncio
    async def test_usd_budget(self):
        run = _make_run(ResourceLimits(max_total_usd=1.0, max_total_tokens=None))
        bus = InMemoryBus()
        gov = ResourceGovernor(run.task.resources, run, bus)
        async with gov:
            gov.charge(usd=0.5)
            assert run.cumulative.usd == 0.5
            with pytest.raises(BudgetExceeded):
                gov.charge(usd=0.6)

    @pytest.mark.asyncio
    async def test_accumulates_multiple_charges(self):
        run = _make_run(ResourceLimits(max_total_tokens=1_000_000))
        bus = InMemoryBus()
        gov = ResourceGovernor(run.task.resources, run, bus)
        async with gov:
            gov.charge(tokens_in=100, tokens_out=50)
            gov.charge(tokens_in=200, tokens_out=100)
            assert run.cumulative.tokens_in == 300
            assert run.cumulative.tokens_out == 150
