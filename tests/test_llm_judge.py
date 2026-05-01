"""Tests for LLMJudgeGate — progress validation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from horizonx.core.types import (
    AgentConfig,
    GateAction,
    Run,
    Session,
    Step,
    StepType,
    StrategyConfig,
    Task,
)
from horizonx.validators.llm_judge import LLMJudgeGate


def _make_run(workspace: Path) -> Run:
    task = Task(
        id="t1", name="Test", prompt="test", description="Test task",
        strategy=StrategyConfig(kind="single"),
        agent=AgentConfig(type="mock", model="mock"),
    )
    return Run(task=task, workspace_path=workspace)


class TestLLMJudge:
    @pytest.mark.asyncio
    async def test_passing_score(self, tmp_path: Path):
        run = _make_run(tmp_path)
        session = Session(run_id=run.id, sequence_index=0)
        store = MagicMock()
        store.recent_steps = AsyncMock(return_value=[
            Step(session_id=session.id, sequence=0, type=StepType.TOOL_CALL,
                 tool_name="Bash", content={"command": "test"}),
        ])

        judge = LLMJudgeGate({"threshold": 0.7}, store=store)
        with patch("horizonx.core.llm_client.call_llm_json", new_callable=AsyncMock) as mock:
            mock.return_value = {
                "score": 0.85,
                "reason": "Great progress",
                "concerns": [],
                "evidence": ["tests pass"],
            }
            decision = await judge.validate(run, session, None)
            assert decision.decision == GateAction.CONTINUE
            assert decision.score == 0.85
            assert decision.duration_ms is not None

    @pytest.mark.asyncio
    async def test_failing_score(self, tmp_path: Path):
        run = _make_run(tmp_path)
        session = Session(run_id=run.id, sequence_index=0)
        store = MagicMock()
        store.recent_steps = AsyncMock(return_value=[])

        judge = LLMJudgeGate({"threshold": 0.7}, store=store)
        with patch("horizonx.core.llm_client.call_llm_json", new_callable=AsyncMock) as mock:
            mock.return_value = {
                "score": 0.3,
                "reason": "No progress",
                "concerns": ["spinning"],
                "evidence": [],
            }
            decision = await judge.validate(run, session, None)
            assert decision.decision == GateAction.PAUSE_FOR_HITL
            assert decision.score == 0.3

    @pytest.mark.asyncio
    async def test_custom_on_fail(self, tmp_path: Path):
        run = _make_run(tmp_path)
        session = Session(run_id=run.id, sequence_index=0)
        store = MagicMock()
        store.recent_steps = AsyncMock(return_value=[])

        judge = LLMJudgeGate(
            {"threshold": 0.7, "on_fail": "abort"},
            store=store,
        )
        with patch("horizonx.core.llm_client.call_llm_json", new_callable=AsyncMock) as mock:
            mock.return_value = {"score": 0.2, "reason": "Bad", "concerns": [], "evidence": []}
            decision = await judge.validate(run, session, None)
            assert decision.decision == GateAction.ABORT

    @pytest.mark.asyncio
    async def test_llm_failure_defaults_continue(self, tmp_path: Path):
        run = _make_run(tmp_path)
        session = Session(run_id=run.id, sequence_index=0)
        store = MagicMock()
        store.recent_steps = AsyncMock(return_value=[])

        judge = LLMJudgeGate({"threshold": 0.7}, store=store)
        with patch("horizonx.core.llm_client.call_llm_json", new_callable=AsyncMock) as mock:
            mock.side_effect = Exception("API down")
            decision = await judge.validate(run, session, None)
            assert decision.decision == GateAction.CONTINUE
            assert "failed" in decision.reason.lower()

    @pytest.mark.asyncio
    async def test_no_session(self, tmp_path: Path):
        run = _make_run(tmp_path)
        judge = LLMJudgeGate({"threshold": 0.7}, store=None)
        decision = await judge.validate(run, None, None)
        assert decision.decision == GateAction.CONTINUE

    @pytest.mark.asyncio
    async def test_no_store(self, tmp_path: Path):
        run = _make_run(tmp_path)
        session = Session(run_id=run.id, sequence_index=0)
        judge = LLMJudgeGate({"threshold": 0.7}, store=None)
        with patch("horizonx.core.llm_client.call_llm_json", new_callable=AsyncMock) as mock:
            mock.return_value = {"score": 0.9, "reason": "OK", "concerns": [], "evidence": []}
            decision = await judge.validate(run, session, None)
            assert decision.decision == GateAction.CONTINUE
            call_args = mock.call_args
            assert "not available" in call_args.kwargs["user_prompt"]

    @pytest.mark.asyncio
    async def test_details_include_metadata(self, tmp_path: Path):
        run = _make_run(tmp_path)
        session = Session(run_id=run.id, sequence_index=0)
        store = MagicMock()
        store.recent_steps = AsyncMock(return_value=[])

        judge = LLMJudgeGate(
            {"threshold": 0.6, "rubric": "custom rubric", "model": "claude-haiku-4-5"},
            store=store,
        )
        with patch("horizonx.core.llm_client.call_llm_json", new_callable=AsyncMock) as mock:
            mock.return_value = {"score": 0.8, "reason": "OK", "concerns": [], "evidence": []}
            decision = await judge.validate(run, session, None)
            assert decision.details["rubric"] == "custom rubric"
            assert decision.details["threshold"] == 0.6
            assert decision.details["model"] == "claude-haiku-4-5"
