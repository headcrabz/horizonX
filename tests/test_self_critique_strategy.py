"""Tests for horizonx.strategies.self_critique."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from horizonx.core.runtime import Runtime
from horizonx.core.types import AgentConfig, RunStatus, StrategyConfig, Task


class TestSelfCritiqueStrategy:
    @pytest.mark.asyncio
    async def test_accepts_on_high_score(self, rt: Runtime, tmp_path: Path):
        task = Task(
            id="sc-test",
            name="SelfCritique test",
            prompt="Refactor the code",
            strategy=StrategyConfig(kind="self_critique", config={
                "max_rounds": 3,
                "accept_threshold": 0.8,
                "critic_type": "llm",
            }),
            agent=AgentConfig(type="mock", model="mock"),
        )
        with patch("horizonx.core.llm_client.call_llm_json", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = {
                "score": 0.9,
                "verdict": "accept",
                "issues": [],
                "suggestions": [],
                "summary": "Excellent refactoring.",
            }
            run = await rt.run(task)
            assert run.status == RunStatus.COMPLETED
            assert mock_llm.call_count == 1

    @pytest.mark.asyncio
    async def test_iterates_until_threshold(self, rt: Runtime, tmp_path: Path):
        task = Task(
            id="sc-test2",
            name="SelfCritique iterate",
            prompt="Fix the bug",
            strategy=StrategyConfig(kind="self_critique", config={
                "max_rounds": 3,
                "accept_threshold": 0.9,
                "critic_type": "llm",
            }),
            agent=AgentConfig(type="mock", model="mock"),
        )
        call_count = 0

        async def mock_llm(**kwargs):
            nonlocal call_count
            call_count += 1
            score = 0.6 if call_count < 3 else 0.95
            return {
                "score": score,
                "verdict": "accept" if score >= 0.9 else "revise",
                "issues": [] if score >= 0.9 else [{"severity": "major", "description": "needs work", "location": "main.py:10"}],
                "suggestions": [],
                "summary": f"Round {call_count}",
            }

        with patch("horizonx.core.llm_client.call_llm_json", new=mock_llm):
            run = await rt.run(task)
            assert run.status == RunStatus.COMPLETED
            assert call_count == 3

    @pytest.mark.asyncio
    async def test_max_rounds_exhausted(self, rt: Runtime, tmp_path: Path):
        task = Task(
            id="sc-test3",
            name="SelfCritique max rounds",
            prompt="Fix it",
            strategy=StrategyConfig(kind="self_critique", config={
                "max_rounds": 2,
                "accept_threshold": 0.99,
                "critic_type": "llm",
            }),
            agent=AgentConfig(type="mock", model="mock"),
        )
        with patch("horizonx.core.llm_client.call_llm_json", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = {
                "score": 0.5,
                "verdict": "revise",
                "issues": [{"severity": "critical", "description": "broken", "location": "x.py:1"}],
                "suggestions": ["rewrite"],
                "summary": "Not there yet.",
            }
            run = await rt.run(task)
            assert run.status == RunStatus.COMPLETED
            assert mock_llm.call_count == 2

    @pytest.mark.asyncio
    async def test_critique_md_written(self, rt: Runtime, tmp_path: Path):
        task = Task(
            id="sc-test4",
            name="SelfCritique writes critique",
            prompt="Improve",
            strategy=StrategyConfig(kind="self_critique", config={
                "max_rounds": 1,
                "accept_threshold": 0.5,
                "critic_type": "llm",
            }),
            agent=AgentConfig(type="mock", model="mock"),
        )
        with patch("horizonx.core.llm_client.call_llm_json", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = {
                "score": 0.8,
                "verdict": "accept",
                "issues": [],
                "suggestions": ["good job"],
                "summary": "Great work!",
            }
            run = await rt.run(task)
            critique_path = run.workspace_path / "critique.md"
            assert critique_path.exists()
            content = critique_path.read_text()
            assert "Round 1" in content
            assert "0.80" in content

    @pytest.mark.asyncio
    async def test_progress_md_written(self, rt: Runtime, tmp_path: Path):
        task = Task(
            id="sc-test5",
            name="SelfCritique progress",
            prompt="Improve",
            strategy=StrategyConfig(kind="self_critique", config={
                "max_rounds": 2,
                "accept_threshold": 0.99,
                "critic_type": "llm",
                "write_progress": True,
            }),
            agent=AgentConfig(type="mock", model="mock"),
        )
        with patch("horizonx.core.llm_client.call_llm_json", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = {
                "score": 0.7, "verdict": "revise",
                "issues": [], "suggestions": [], "summary": "Keep going.",
            }
            run = await rt.run(task)
            progress_path = run.workspace_path / "progress.md"
            assert progress_path.exists()
            content = progress_path.read_text()
            assert "Round 1" in content
            assert "Round 2" in content

    @pytest.mark.asyncio
    async def test_example_yaml_loads(self):
        import yaml
        yaml_path = Path(__file__).parent.parent / "examples" / "self_critique" / "task.yaml"
        with open(yaml_path) as f:
            data = yaml.safe_load(f)
        task = Task(**data)
        assert task.strategy.kind == "self_critique"
        assert task.strategy.config["max_rounds"] == 5
        assert task.strategy.config["critic_type"] == "llm"

    def test_shell_critic_format(self):
        from horizonx.strategies.self_critique import SelfCritique
        sc = SelfCritique({"max_rounds": 3, "critic_type": "llm"})
        critique_md = sc._format_critique({
            "score": 0.75,
            "verdict": "revise",
            "issues": [{"severity": "major", "description": "missing types", "location": "src/a.py:5"}],
            "suggestions": ["Add type annotations"],
            "summary": "Needs improvement.",
        }, round_n=0)
        assert "Round 1" in critique_md
        assert "0.75" in critique_md
        assert "major" in critique_md.lower()
        assert "Add type annotations" in critique_md
