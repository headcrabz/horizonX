"""End-to-end tests — load real example task.yaml files, swap agent to mock, run pipeline."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from horizonx.core.runtime import Runtime
from horizonx.core.types import RunStatus, Task
from horizonx.storage.sqlite import SqliteStore

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"


def _load_task_as_mock(yaml_path: Path) -> Task:
    """Load a task.yaml and override the agent to mock for testing."""
    with open(yaml_path) as f:
        data = yaml.safe_load(f)
    data["agent"] = {"type": "mock", "model": "mock", "extra": {
        "steps": [
            {"type": "thought", "content": {"text": "Planning the implementation..."}},
            {"type": "tool_call", "tool_name": "Bash", "content": {"command": "echo starting"}},
            {"type": "observation", "tool_name": "Bash", "content": {"output": "starting"}},
            {"type": "tool_call", "tool_name": "Edit", "content": {"file_path": "main.py", "change": "add code"}},
            {"type": "observation", "tool_name": "Edit", "content": {"output": "file edited"}},
            {"type": "thought", "content": {"text": "Implementation complete."}},
        ],
        "status": "completed",
    }}
    # Remove validators that require real commands for mock testing
    data["milestone_validators"] = []
    # Override strategy to single for faster e2e
    data["strategy"] = {"kind": "single"}
    return Task(**data)


@pytest.fixture
def rt(tmp_path: Path) -> Runtime:
    store = SqliteStore(tmp_path / "e2e.db")
    return Runtime(store=store, workspace_root=tmp_path / "ws")


class TestExampleConfigs:
    """Verify all example task.yaml files parse correctly."""

    @pytest.mark.parametrize("example", [
        "autoresearch", "autotrain", "coding", "data_analysis", "kernel_optimization",
    ])
    def test_yaml_validates(self, example: str):
        yaml_path = EXAMPLES_DIR / example / "task.yaml"
        assert yaml_path.exists(), f"Missing {yaml_path}"
        with open(yaml_path) as f:
            data = yaml.safe_load(f)
        task = Task(**data)
        assert task.id
        assert task.name
        assert task.strategy.kind in ("single", "sequential", "ralph", "tree", "monitor")


class TestE2EPipeline:
    """Run each example through the full pipeline with MockAgent."""

    @pytest.mark.asyncio
    async def test_coding_example(self, rt: Runtime):
        task = _load_task_as_mock(EXAMPLES_DIR / "coding" / "task.yaml")
        run = await rt.run(task)
        assert run.status == RunStatus.COMPLETED
        assert run.cumulative.sessions_count == 1
        steps = await rt.store.recent_steps(run.current_session_id, 100)
        assert len(steps) == 6

    @pytest.mark.asyncio
    async def test_autoresearch_example(self, rt: Runtime):
        task = _load_task_as_mock(EXAMPLES_DIR / "autoresearch" / "task.yaml")
        run = await rt.run(task)
        assert run.status == RunStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_autotrain_example(self, rt: Runtime):
        task = _load_task_as_mock(EXAMPLES_DIR / "autotrain" / "task.yaml")
        run = await rt.run(task)
        assert run.status == RunStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_data_analysis_example(self, rt: Runtime):
        task = _load_task_as_mock(EXAMPLES_DIR / "data_analysis" / "task.yaml")
        run = await rt.run(task)
        assert run.status == RunStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_kernel_optimization_example(self, rt: Runtime):
        task = _load_task_as_mock(EXAMPLES_DIR / "kernel_optimization" / "task.yaml")
        run = await rt.run(task)
        assert run.status == RunStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_steps_persisted_to_sqlite(self, rt: Runtime):
        task = _load_task_as_mock(EXAMPLES_DIR / "coding" / "task.yaml")
        run = await rt.run(task)
        steps = await rt.store.recent_steps(run.current_session_id, 100)
        assert len(steps) > 0
        assert any(s.type.value == "thought" for s in steps)
        assert any(s.type.value == "tool_call" for s in steps)

    @pytest.mark.asyncio
    async def test_run_persisted_to_sqlite(self, rt: Runtime):
        task = _load_task_as_mock(EXAMPLES_DIR / "coding" / "task.yaml")
        run = await rt.run(task)
        loaded = await rt.store.load_run(run.id)
        assert loaded.status == RunStatus.COMPLETED
        assert loaded.id == run.id

    @pytest.mark.asyncio
    async def test_with_llm_judge_validator(self, rt: Runtime):
        """Run with an LLM judge validator (mocked LLM)."""
        yaml_path = EXAMPLES_DIR / "coding" / "task.yaml"
        with open(yaml_path) as f:
            data = yaml.safe_load(f)
        data["agent"] = {"type": "mock", "model": "mock"}
        data["strategy"] = {"kind": "single"}
        data["milestone_validators"] = [{
            "id": "progress_judge",
            "type": "llm_judge",
            "runs": "final",
            "config": {"threshold": 0.6, "rubric": "Did the agent make progress on OAuth?"},
        }]
        task = Task(**data)

        with patch("horizonx.core.llm_client.call_llm_json", new_callable=AsyncMock) as mock:
            mock.return_value = {
                "score": 0.85,
                "reason": "Good progress on OAuth implementation",
                "concerns": [],
                "evidence": ["code files created"],
            }
            run = await rt.run(task)
            assert run.status == RunStatus.COMPLETED
            mock.assert_called_once()
