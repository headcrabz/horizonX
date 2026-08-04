"""Spin analysis consumes canonical provider events, not adapter payload shapes."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from horizonx.core.spin_detector import EditRevertLayer, ToolThrashingLayer
from horizonx.core.types import Session, Step, StepType


def _canonical_step(seq: int, *, category: str, target: str, changed: str | None = None, result: str | None = None) -> Step:
    return Step(
        session_id="s1", sequence=seq, type=StepType.TOOL_CALL, tool_name="adapter-specific",
        content={}, canonical={"kind": "tool_call", "category": category, "target": target,
                               "changed_file_digest": changed, "result_digest": result},
    )


@pytest.mark.asyncio
async def test_edit_revert_detects_actual_a_to_b_to_a_from_canonical_digests() -> None:
    store = MagicMock()
    store.recent_steps = AsyncMock(return_value=[
        _canonical_step(1, category="edit", target="app.py", changed="A"),
        _canonical_step(2, category="edit", target="app.py", changed="B"),
        _canonical_step(3, category="edit", target="app.py", changed="A"),
    ])

    report = await EditRevertLayer().check(Session(run_id="r1", sequence_index=0), store)

    assert report.detected


@pytest.mark.asyncio
async def test_repeated_polling_with_distinct_result_digests_is_not_thrashing() -> None:
    store = MagicMock()
    store.recent_steps = AsyncMock(return_value=[
        _canonical_step(i, category="read", target="/status", result=f"digest-{i}") for i in range(5)
    ])

    report = await ToolThrashingLayer(no_progress_threshold=5).check(
        Session(run_id="r1", sequence_index=0), store
    )

    assert not report.detected
