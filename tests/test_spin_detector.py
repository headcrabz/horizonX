"""Tests for spin detection layers and SpinDetector."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from horizonx.core.spin_detector import (
    BucketedHashLayer,
    EditRevertLayer,
    ExactLoopLayer,
    ScorePlateauLayer,
    SemanticProgressLayer,
    SpinDetector,
    ToolThrashingLayer,
)
from horizonx.core.types import Session, SpinDetectionConfig, Step, StepType


def _tool_step(seq: int, tool: str = "Bash", content: dict | None = None) -> Step:
    return Step(
        session_id="s1",
        sequence=seq,
        type=StepType.TOOL_CALL,
        tool_name=tool,
        content=content or {"command": f"echo {seq}"},
    )


class TestExactLoopLayer:
    @pytest.mark.asyncio
    async def test_no_spin(self):
        store = MagicMock()
        store.recent_steps = AsyncMock(return_value=[
            _tool_step(i, content={"command": f"unique_{i}"}) for i in range(10)
        ])
        layer = ExactLoopLayer(threshold=3, window=20)
        session = Session(run_id="r1", sequence_index=0)
        report = await layer.check(session, store)
        assert not report.detected

    @pytest.mark.asyncio
    async def test_spin_detected(self):
        store = MagicMock()
        repeated = {"command": "npm test"}
        store.recent_steps = AsyncMock(return_value=[
            _tool_step(i, content=repeated) for i in range(5)
        ])
        layer = ExactLoopLayer(threshold=3, window=20)
        session = Session(run_id="r1", sequence_index=0)
        report = await layer.check(session, store)
        assert report.detected
        assert report.layer == "exact_loop"

    @pytest.mark.asyncio
    async def test_below_soft_threshold(self):
        # count=1 is below both soft=2 and hard=3 — no detection at all
        store = MagicMock()
        repeated = {"command": "npm test"}
        steps = [_tool_step(0, content=repeated)]
        steps.append(_tool_step(1, content={"command": "other"}))
        store.recent_steps = AsyncMock(return_value=steps)
        layer = ExactLoopLayer(threshold=3, window=20)
        session = Session(run_id="r1", sequence_index=0)
        report = await layer.check(session, store)
        assert not report.detected

    @pytest.mark.asyncio
    async def test_soft_threshold_warns_not_aborts(self):
        # count=2 == soft=2 fires a soft warning, NOT a hard abort
        store = MagicMock()
        repeated = {"command": "npm test"}
        steps = [_tool_step(0, content=repeated), _tool_step(1, content=repeated)]
        store.recent_steps = AsyncMock(return_value=steps)
        layer = ExactLoopLayer(threshold=3, window=20)
        session = Session(run_id="r1", sequence_index=0)
        report = await layer.check(session, store)
        assert report.detected
        assert report.detail["tier"] == "soft"
        assert report.action == "warn_and_inject_diagnostic"


class TestEditRevertLayer:
    @pytest.mark.asyncio
    async def test_no_revert(self):
        store = MagicMock()
        store.recent_steps = AsyncMock(return_value=[
            Step(session_id="s1", sequence=i, type=StepType.TOOL_CALL,
                 tool_name="Edit", content={"file_path": "a.py", "change": f"v{i}"})
            for i in range(4)
        ])
        layer = EditRevertLayer()
        session = Session(run_id="r1", sequence_index=0)
        report = await layer.check(session, store)
        assert not report.detected

    @pytest.mark.asyncio
    async def test_abab_pattern(self):
        store = MagicMock()
        edit_a = {"file_path": "a.py", "old": "x", "new": "y"}
        edit_b = {"file_path": "a.py", "old": "y", "new": "x"}
        steps = [
            Step(session_id="s1", sequence=0, type=StepType.TOOL_CALL, tool_name="Edit", content=edit_a),
            Step(session_id="s1", sequence=1, type=StepType.TOOL_CALL, tool_name="Edit", content=edit_b),
            Step(session_id="s1", sequence=2, type=StepType.TOOL_CALL, tool_name="Edit", content=edit_a),
            Step(session_id="s1", sequence=3, type=StepType.TOOL_CALL, tool_name="Edit", content=edit_b),
        ]
        store.recent_steps = AsyncMock(return_value=steps)
        layer = EditRevertLayer()
        session = Session(run_id="r1", sequence_index=0)
        report = await layer.check(session, store)
        assert report.detected
        assert report.layer == "edit_revert"


class TestScorePlateauLayer:
    @pytest.mark.asyncio
    async def test_no_plateau(self):
        store = MagicMock()
        store.recent_validator_scores = AsyncMock(return_value=[0.5, 0.7, 0.85])
        layer = ScorePlateauLayer(window=3, delta=0.02)
        session = Session(run_id="r1", sequence_index=0)
        report = await layer.check(session, store)
        assert not report.detected

    @pytest.mark.asyncio
    async def test_plateau_detected(self):
        store = MagicMock()
        store.recent_validator_scores = AsyncMock(return_value=[0.70, 0.71, 0.70])
        layer = ScorePlateauLayer(window=3, delta=0.02)
        session = Session(run_id="r1", sequence_index=0)
        report = await layer.check(session, store)
        assert report.detected
        assert report.layer == "score_plateau"

    @pytest.mark.asyncio
    async def test_insufficient_scores(self):
        store = MagicMock()
        store.recent_validator_scores = AsyncMock(return_value=[0.5])
        layer = ScorePlateauLayer(window=3, delta=0.02)
        session = Session(run_id="r1", sequence_index=0)
        report = await layer.check(session, store)
        assert not report.detected


class TestToolThrashingLayer:
    @pytest.mark.asyncio
    async def test_no_thrashing(self):
        store = MagicMock()
        tools = ["Bash", "Edit", "Read", "Bash", "Edit", "Read", "Bash", "Edit", "Read", "Bash",
                 "Edit", "Read", "Bash", "Edit", "Read", "Bash", "Edit", "Read", "Bash", "Edit"]
        store.recent_steps = AsyncMock(return_value=[
            _tool_step(i, tool=t) for i, t in enumerate(tools)
        ])
        layer = ToolThrashingLayer()
        session = Session(run_id="r1", sequence_index=0)
        report = await layer.check(session, store)
        assert not report.detected

    @pytest.mark.asyncio
    async def test_thrashing_detected(self):
        store = MagicMock()
        store.recent_steps = AsyncMock(return_value=[
            _tool_step(i, tool="Bash") for i in range(25)
        ])
        layer = ToolThrashingLayer()
        session = Session(run_id="r1", sequence_index=0)
        report = await layer.check(session, store)
        assert report.detected
        assert report.layer == "tool_thrashing"


class TestSemanticProgressLayer:
    @pytest.mark.asyncio
    async def test_skips_when_not_at_interval(self):
        layer = SemanticProgressLayer(every_n=20)
        session = Session(run_id="r1", sequence_index=0, steps_count=10)
        report = await layer.check(session, MagicMock())
        assert not report.detected

    @pytest.mark.asyncio
    async def test_calls_llm_at_interval(self):
        store = MagicMock()
        store.recent_steps = AsyncMock(return_value=[
            _tool_step(i) for i in range(20)
        ])
        layer = SemanticProgressLayer(every_n=20)
        session = Session(run_id="r1", sequence_index=0, steps_count=20)

        with patch("horizonx.core.llm_client.call_llm_json", new_callable=AsyncMock) as mock:
            mock.return_value = {"spinning": False, "confidence": 0.9, "reason": "ok"}
            report = await layer.check(session, store)
            assert not report.detected
            mock.assert_called_once()


class TestSpinDetectorIntegration:
    @pytest.mark.asyncio
    async def test_no_spin(self):
        store = MagicMock()
        store.recent_steps = AsyncMock(return_value=[
            _tool_step(i, content={"command": f"unique_{i}"}) for i in range(5)
        ])
        store.recent_validator_scores = AsyncMock(return_value=[0.5, 0.7, 0.85])
        config = SpinDetectionConfig(semantic_layer_enabled=False)
        detector = SpinDetector(config, store)
        session = Session(run_id="r1", sequence_index=0, steps_count=5)
        report = await detector.check(session)
        assert not report.detected

    @pytest.mark.asyncio
    async def test_first_layer_fires(self):
        store = MagicMock()
        store.recent_steps = AsyncMock(return_value=[
            _tool_step(i, content={"command": "same"}) for i in range(5)
        ])
        store.recent_validator_scores = AsyncMock(return_value=[])
        config = SpinDetectionConfig(
            exact_loop_threshold=3,
            semantic_layer_enabled=False,
        )
        detector = SpinDetector(config, store)
        session = Session(run_id="r1", sequence_index=0, steps_count=5)
        report = await detector.check(session)
        assert report.detected
        assert report.layer == "exact_loop"


# ---------------------------------------------------------------------------
# FakeStore helper for dual-threshold tests
# ---------------------------------------------------------------------------

class FakeStore:
    def __init__(self, steps: list[Step]):
        self._steps = steps

    async def recent_steps(self, session_id: str, n: int) -> list[Step]:
        return self._steps[-n:]

    async def recent_validator_scores(self, *args: Any, **kwargs: Any) -> list[float]:
        return []


# ---------------------------------------------------------------------------
# Dual-threshold ExactLoopLayer
# ---------------------------------------------------------------------------

class TestExactLoopLayerDualThreshold:
    def _session(self) -> Session:
        return Session(run_id="run-test", sequence_index=0)

    @pytest.mark.asyncio
    async def test_no_spin_below_soft(self):
        layer = ExactLoopLayer(hard_threshold=3, soft_threshold=2, window=20)
        step = _tool_step(0, tool="Bash", content={"command": "ls"})
        store = FakeStore([step])  # count=1 < soft=2
        report = await layer.check(self._session(), store)
        assert not report.detected

    @pytest.mark.asyncio
    async def test_soft_fires_warn_not_abort(self):
        layer = ExactLoopLayer(hard_threshold=4, soft_threshold=2, window=20)
        step = _tool_step(0, tool="Bash", content={"command": "ls"})
        store = FakeStore([step, step])  # count=2 == soft=2
        report = await layer.check(self._session(), store)
        assert report.detected
        assert report.detail["tier"] == "soft"
        assert report.action == "warn_and_inject_diagnostic"

    @pytest.mark.asyncio
    async def test_hard_fires_abort(self):
        layer = ExactLoopLayer(hard_threshold=3, soft_threshold=2, window=20)
        step = _tool_step(0, tool="Bash", content={"command": "ls"})
        store = FakeStore([step, step, step])  # count=3 == hard=3
        report = await layer.check(self._session(), store)
        assert report.detected
        assert report.detail["tier"] == "hard"
        assert report.action == "terminate_session_and_retry"

    @pytest.mark.asyncio
    async def test_no_spin_on_varied_calls(self):
        layer = ExactLoopLayer(hard_threshold=3, soft_threshold=2, window=20)
        steps = [_tool_step(i, tool="Bash", content={"command": f"cmd{i}"}) for i in range(6)]
        store = FakeStore(steps)
        report = await layer.check(self._session(), store)
        assert not report.detected


# ---------------------------------------------------------------------------
# BucketedHashLayer
# ---------------------------------------------------------------------------

class TestBucketedHashLayer:
    def _session(self) -> Session:
        return Session(run_id="run-test", sequence_index=0)

    @pytest.mark.asyncio
    async def test_no_spin_below_soft(self):
        layer = BucketedHashLayer(soft_threshold=3, hard_threshold=5, window=30)
        steps = [_tool_step(i, tool="Bash", content={"command": "ls"}) for i in range(2)]
        store = FakeStore(steps)
        report = await layer.check(self._session(), store)
        assert not report.detected

    @pytest.mark.asyncio
    async def test_soft_fires_at_soft_threshold(self):
        layer = BucketedHashLayer(soft_threshold=3, hard_threshold=5, window=30)
        step = _tool_step(0, tool="Bash", content={"command": "ls -la"})
        store = FakeStore([step] * 3)
        report = await layer.check(self._session(), store)
        assert report.detected
        assert report.detail["tier"] == "soft"
        assert report.action == "warn_and_inject_diagnostic"

    @pytest.mark.asyncio
    async def test_hard_fires_at_hard_threshold(self):
        layer = BucketedHashLayer(soft_threshold=3, hard_threshold=5, window=30)
        step = _tool_step(0, tool="Bash", content={"command": "ls -la"})
        store = FakeStore([step] * 5)
        report = await layer.check(self._session(), store)
        assert report.detected
        assert report.detail["tier"] == "hard"
        assert report.action == "terminate_session_and_retry"

    @pytest.mark.asyncio
    async def test_no_tool_calls_no_spin(self):
        layer = BucketedHashLayer(soft_threshold=3, hard_threshold=5, window=30)
        steps = [Step(session_id="s", sequence=i, type=StepType.THOUGHT, content={"text": "x"}) for i in range(10)]
        store = FakeStore(steps)
        report = await layer.check(self._session(), store)
        assert not report.detected

    @pytest.mark.asyncio
    async def test_bucket_tolerates_minor_variation(self):
        layer = BucketedHashLayer(soft_threshold=2, hard_threshold=4, window=30)
        s1 = _tool_step(0, tool="Bash", content={"command": "pytest tests/"})
        s2 = _tool_step(1, tool="Bash", content={"command": "pytest tests/"})
        store = FakeStore([s1, s2])
        report = await layer.check(self._session(), store)
        assert report.detected  # hits soft at count=2


# ---------------------------------------------------------------------------
# SpinDetectionConfig new-field defaults
# ---------------------------------------------------------------------------

class TestSpinDetectionConfigDefaults:
    def test_new_fields_have_defaults(self):
        cfg = SpinDetectionConfig()
        assert cfg.soft_exact_loop_threshold == 2
        assert cfg.bucketed_hash_enabled is True
        assert cfg.bucketed_hash_soft_threshold == 3
        assert cfg.bucketed_hash_hard_threshold == 5
        assert cfg.bucketed_hash_window == 30
