"""Shared fixtures for HorizonX tests."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

from horizonx.core.runtime import Runtime
from horizonx.core.types import (
    AgentConfig,
    StrategyConfig,
    Task,
    ValidatorConfig,
)
from horizonx.storage.sqlite import SqliteStore


@pytest.fixture
def rt(tmp_path: Path) -> Runtime:
    store = SqliteStore(tmp_path / "test.db")
    return Runtime(store=store, workspace_root=tmp_path / "ws")


@pytest.fixture
def tmp_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def store(tmp_path: Path) -> SqliteStore:
    return SqliteStore(tmp_path / "test.db")


@pytest.fixture
def mock_task() -> Task:
    return Task(
        id="test-task",
        name="Test task",
        description="A test task for unit tests",
        prompt="Do nothing",
        strategy=StrategyConfig(kind="single"),
        agent=AgentConfig(type="mock", model="mock"),
    )


@pytest.fixture
def mock_task_with_judge() -> Task:
    return Task(
        id="test-judge-task",
        name="Judge test",
        description="Test with LLM judge",
        prompt="Do something",
        strategy=StrategyConfig(kind="single"),
        agent=AgentConfig(type="mock", model="mock"),
        milestone_validators=[
            ValidatorConfig(
                id="judge1",
                type="llm_judge",
                runs="final",
                config={"threshold": 0.7, "rubric": "Did the task complete?"},
            ),
        ],
    )
