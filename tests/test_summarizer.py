"""Tests for Summarizer — structured handoff generation."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from horizonx.core.summarizer import Summarizer
from horizonx.core.types import (
    AgentConfig,
    GoalNode,
    Run,
    Session,
    Step,
    StepType,
    StrategyConfig,
    SummarizerConfig,
    Task,
)


def _make_run(workspace: Path) -> Run:
    task = Task(
        id="t1", name="Test", prompt="test",
        strategy=StrategyConfig(kind="single"),
        agent=AgentConfig(type="mock", model="mock"),
    )
    return Run(task=task, workspace_path=workspace)


def _make_steps(session_id: str, n: int = 5) -> list[Step]:
    steps = []
    for i in range(n):
        if i % 3 == 0:
            steps.append(Step(session_id=session_id, sequence=i,
                              type=StepType.THOUGHT, content={"text": f"thinking {i}"}))
        elif i % 3 == 1:
            steps.append(Step(session_id=session_id, sequence=i,
                              type=StepType.TOOL_CALL, tool_name="Bash",
                              content={"command": f"echo {i}"}))
        else:
            steps.append(Step(session_id=session_id, sequence=i,
                              type=StepType.OBSERVATION, tool_name="Bash",
                              content={"output": f"output {i}"}))
    return steps


class TestSummarizer:
    @pytest.mark.asyncio
    async def test_produces_summary_file(self, tmp_path: Path):
        run = _make_run(tmp_path)
        session = Session(run_id=run.id, sequence_index=0)
        store = MagicMock()
        store.recent_steps = AsyncMock(return_value=_make_steps(session.id))
        store.load_goal = AsyncMock(return_value=None)

        summarizer = Summarizer(SummarizerConfig(), store)

        with patch("horizonx.core.llm_client.call_llm_json", new_callable=AsyncMock) as mock:
            mock.return_value = {
                "summary_md": "Implemented auth middleware.",
                "key_decisions": ["Used JWT over session cookies"],
                "blockers": [],
                "next_actions": ["Add token refresh"],
                "files_modified": ["auth.py"],
                "tests_status": {"passing": 3},
                "confidence": 0.8,
            }
            path = await summarizer.summarize(session, run)
            assert path.exists()
            content = path.read_text()
            assert "auth middleware" in content
            assert "JWT" in content

    @pytest.mark.asyncio
    async def test_disabled_returns_empty(self, tmp_path: Path):
        run = _make_run(tmp_path)
        session = Session(run_id=run.id, sequence_index=0)
        summarizer = Summarizer(SummarizerConfig(enabled=False), MagicMock())
        path = await summarizer.summarize(session, run)
        assert path == Path()

    @pytest.mark.asyncio
    async def test_fallback_on_llm_failure(self, tmp_path: Path):
        run = _make_run(tmp_path)
        session = Session(run_id=run.id, sequence_index=0)
        store = MagicMock()
        store.recent_steps = AsyncMock(return_value=_make_steps(session.id, 10))
        store.load_goal = AsyncMock(return_value=None)

        summarizer = Summarizer(SummarizerConfig(), store)

        with patch("horizonx.core.llm_client.call_llm_json", new_callable=AsyncMock) as mock:
            mock.return_value = {"error": "json_parse_failed", "raw": "garbage"}
            path = await summarizer.summarize(session, run)
            assert path.exists()
            content = path.read_text()
            assert "fallback" in content.lower()

    @pytest.mark.asyncio
    async def test_with_goal(self, tmp_path: Path):
        run = _make_run(tmp_path)
        session = Session(run_id=run.id, sequence_index=0, target_goal_id="g.auth")
        goal = GoalNode(id="g.auth", name="Auth system", description="Build OAuth 2.0")
        store = MagicMock()
        store.recent_steps = AsyncMock(return_value=_make_steps(session.id))
        store.load_goal = AsyncMock(return_value=goal)

        summarizer = Summarizer(SummarizerConfig(), store)

        with patch("horizonx.core.llm_client.call_llm_json", new_callable=AsyncMock) as mock:
            mock.return_value = {
                "summary_md": "Auth progress",
                "key_decisions": [],
                "blockers": [],
                "next_actions": [],
                "files_modified": [],
                "tests_status": {},
                "confidence": 0.6,
            }
            path = await summarizer.summarize(session, run)
            # Verify the prompt included the goal info
            call_args = mock.call_args
            assert "Auth system" in call_args.kwargs["user_prompt"]
            assert "OAuth" in call_args.kwargs["user_prompt"]


class TestStepCompression:
    def test_compress_types(self, tmp_path: Path):
        run = _make_run(tmp_path)
        summarizer = Summarizer(SummarizerConfig(), MagicMock())
        steps = [
            Step(session_id="s1", sequence=0, type=StepType.THOUGHT,
                 content={"text": "thinking hard"}),
            Step(session_id="s1", sequence=1, type=StepType.TOOL_CALL,
                 tool_name="Bash", content={"command": "make build"}),
            Step(session_id="s1", sequence=2, type=StepType.OBSERVATION,
                 tool_name="Bash", content={"output": "ok", "is_error": False}),
            Step(session_id="s1", sequence=3, type=StepType.FILE_CHANGE,
                 tool_name="file_change",
                 content={"changes": [{"path": "a.py", "kind": "add"}]}),
            Step(session_id="s1", sequence=4, type=StepType.ERROR,
                 content={"error": "timeout"}),
            Step(session_id="s1", sequence=5, type=StepType.USAGE,
                 content={"input_tokens": 100}),
        ]
        text = summarizer._compress_steps(steps)
        assert "THOUGHT" in text
        assert "CALL Bash" in text
        assert "OBS Bash" in text
        assert "FILE_CHANGE" in text
        assert "ERROR" in text
        # USAGE should be skipped
        assert "USAGE" not in text
