"""Tests for re_decompose HITL action in SequentialSubgoals."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from horizonx.core.types import (
    AgentConfig,
    GoalNode,
    Run,
    RunStatus,
    StrategyConfig,
    Task,
)
from horizonx.strategies.sequential import SequentialSubgoals


def _make_task() -> Task:
    return Task(
        id="test-task",
        name="Build a REST API",
        description="A task for testing re_decompose",
        prompt="Build a REST API with authentication and rate limiting.",
        strategy=StrategyConfig(kind="sequential"),
        agent=AgentConfig(type="mock", model="mock"),
    )


def _make_run(tmp_path: Path, task: Task) -> Run:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return Run(
        task=task,
        status=RunStatus.RUNNING,
        workspace_path=workspace,
    )


def _write_goals(workspace: Path, goals: dict) -> None:
    (workspace / "goals.json").write_text(json.dumps(goals))


def _minimal_goals_with_done_and_pending() -> dict:
    return {
        "version": 1,
        "root": "g.root",
        "nodes": {
            "g.root": {
                "id": "g.root",
                "name": "root",
                "description": "root task",
                "children": ["g.done", "g.pending"],
                "status": "done",
                "attempts": 1,
                "notes": "",
                "verification_criteria": [],
                "parent_id": None,
                "depends_on": [],
                "max_attempts": 3,
                "progress_pct": 100.0,
                "version": 1,
                "last_updated_by_session": None,
            },
            "g.done": {
                "id": "g.done",
                "parent_id": "g.root",
                "name": "done task",
                "description": "already completed",
                "children": [],
                "status": "done",
                "attempts": 1,
                "notes": "finished",
                "verification_criteria": [],
                "depends_on": [],
                "max_attempts": 3,
                "progress_pct": 100.0,
                "version": 1,
                "last_updated_by_session": None,
            },
            "g.pending": {
                "id": "g.pending",
                "parent_id": "g.root",
                "name": "pending task",
                "description": "not yet done",
                "children": [],
                "status": "pending",
                "attempts": 0,
                "notes": "",
                "verification_criteria": [],
                "depends_on": [],
                "max_attempts": 3,
                "progress_pct": 0.0,
                "version": 0,
                "last_updated_by_session": None,
            },
        },
    }


def _make_rt_mock(tmp_path: Path) -> MagicMock:
    from horizonx.core.event_bus import InMemoryBus
    from horizonx.storage.sqlite import SqliteStore

    store = SqliteStore(tmp_path / "test.db")
    bus = InMemoryBus()
    rt = MagicMock()
    rt.store = store
    rt.bus = bus
    return rt


@pytest.mark.asyncio
async def test_re_decompose_preserves_done_goals(tmp_path: Path):
    """DONE goals must remain untouched; pending goals get restructured."""
    task = _make_task()
    run = _make_run(tmp_path, task)
    _write_goals(run.workspace_path, _minimal_goals_with_done_and_pending())

    rt = _make_rt_mock(tmp_path)

    llm_response = {
        "nodes": {
            "g.root": {
                "name": "root",
                "description": "root task",
                "children": ["g.new_sub1", "g.new_sub2"],
                "status": "pending",
                "attempts": 0,
                "notes": "",
                "verification_criteria": [],
            },
            "g.new_sub1": {
                "parent_id": "g.root",
                "name": "New subtask 1",
                "description": "First restructured subtask",
                "children": [],
                "status": "pending",
                "attempts": 0,
                "notes": "",
                "verification_criteria": ["subtask 1 complete"],
            },
            "g.new_sub2": {
                "parent_id": "g.root",
                "name": "New subtask 2",
                "description": "Second restructured subtask",
                "children": [],
                "status": "pending",
                "attempts": 0,
                "notes": "",
                "verification_criteria": ["subtask 2 complete"],
            },
        }
    }

    strategy = SequentialSubgoals(config={})
    current_goal = GoalNode(
        id="g.pending",
        parent_id="g.root",
        name="pending task",
        description="not yet done",
    )

    with patch(
        "horizonx.strategies.sequential.call_llm_json",
        new=AsyncMock(return_value=llm_response),
    ):
        await strategy._re_decompose(run, rt, current_goal, "Split the pending work into two smaller tasks")

    # Reload from disk and verify
    saved_path = run.workspace_path / "goals.json"
    assert saved_path.exists()
    reloaded = json.loads(saved_path.read_text())

    nodes = reloaded["nodes"]

    # The DONE node must still be present and still be done
    assert "g.done" in nodes, "g.done should be preserved in merged graph"
    assert nodes["g.done"]["status"] == "done"

    # The new nodes should be present
    assert "g.new_sub1" in nodes
    assert "g.new_sub2" in nodes
    assert nodes["g.new_sub1"]["status"] == "pending"
    assert nodes["g.new_sub2"]["status"] == "pending"


@pytest.mark.asyncio
async def test_re_decompose_noop_when_no_goals_file(tmp_path: Path):
    """_re_decompose returns silently when goals.json doesn't exist."""
    task = _make_task()
    run = _make_run(tmp_path, task)
    # Do NOT write goals.json

    rt = _make_rt_mock(tmp_path)
    strategy = SequentialSubgoals(config={})
    current_goal = GoalNode(
        id="g.pending",
        parent_id="g.root",
        name="pending task",
        description="not yet done",
    )

    # Should not raise
    with patch(
        "horizonx.strategies.sequential.call_llm_json",
        new=AsyncMock(return_value={"nodes": {}}),
    ) as mock_llm:
        await strategy._re_decompose(run, rt, current_goal, "restructure")

    # LLM should not even be called since goals.json is absent
    mock_llm.assert_not_called()


@pytest.mark.asyncio
async def test_re_decompose_noop_when_all_goals_done(tmp_path: Path):
    """_re_decompose returns silently when all goals are already DONE."""
    task = _make_task()
    run = _make_run(tmp_path, task)

    all_done = {
        "version": 1,
        "root": "g.root",
        "nodes": {
            "g.root": {
                "id": "g.root",
                "name": "root",
                "description": "root task",
                "children": [],
                "status": "done",
                "attempts": 1,
                "notes": "",
                "verification_criteria": [],
                "parent_id": None,
                "depends_on": [],
                "max_attempts": 3,
                "progress_pct": 100.0,
                "version": 1,
                "last_updated_by_session": None,
            }
        },
    }
    _write_goals(run.workspace_path, all_done)

    rt = _make_rt_mock(tmp_path)
    strategy = SequentialSubgoals(config={})
    current_goal = GoalNode(
        id="g.root",
        name="root",
        description="root task",
    )

    with patch(
        "horizonx.strategies.sequential.call_llm_json",
        new=AsyncMock(return_value={"nodes": {}}),
    ) as mock_llm:
        await strategy._re_decompose(run, rt, current_goal, "restructure")

    # No restructurable goals — LLM should not be called
    mock_llm.assert_not_called()


@pytest.mark.asyncio
async def test_re_decompose_retries_on_empty_llm_response(tmp_path: Path):
    """When LLM returns empty nodes dict, method retries up to 2 times and returns gracefully."""
    task = _make_task()
    run = _make_run(tmp_path, task)
    _write_goals(run.workspace_path, _minimal_goals_with_done_and_pending())

    rt = _make_rt_mock(tmp_path)
    strategy = SequentialSubgoals(config={})
    current_goal = GoalNode(
        id="g.pending",
        parent_id="g.root",
        name="pending task",
        description="not yet done",
    )

    # Always return empty nodes — simulates an LLM that fails to produce output
    with patch(
        "horizonx.strategies.sequential.call_llm_json",
        new=AsyncMock(return_value={"nodes": {}}),
    ) as mock_llm:
        # Should not raise even after 2 failed attempts
        await strategy._re_decompose(run, rt, current_goal, "restructure")

    assert mock_llm.call_count == 2  # exactly 2 attempts


@pytest.mark.asyncio
async def test_re_decompose_publishes_event(tmp_path: Path):
    """A goals.re_decomposed event should be published on success."""
    task = _make_task()
    run = _make_run(tmp_path, task)
    _write_goals(run.workspace_path, _minimal_goals_with_done_and_pending())

    rt = _make_rt_mock(tmp_path)
    published_events = []

    async def capture_publish(event):
        published_events.append(event)

    rt.bus.publish = capture_publish

    llm_response = {
        "nodes": {
            "g.root": {
                "name": "root",
                "description": "root task",
                "children": ["g.sub"],
                "status": "pending",
                "attempts": 0,
                "notes": "",
                "verification_criteria": [],
            },
            "g.sub": {
                "parent_id": "g.root",
                "name": "New sub",
                "description": "new sub goal",
                "children": [],
                "status": "pending",
                "attempts": 0,
                "notes": "",
                "verification_criteria": [],
            },
        }
    }

    strategy = SequentialSubgoals(config={})
    current_goal = GoalNode(
        id="g.pending",
        parent_id="g.root",
        name="pending task",
        description="not yet done",
    )

    with patch(
        "horizonx.strategies.sequential.call_llm_json",
        new=AsyncMock(return_value=llm_response),
    ):
        await strategy._re_decompose(run, rt, current_goal, "Simplify the plan")

    assert len(published_events) == 1
    evt = published_events[0]
    assert evt.type == "goals.re_decomposed"
    assert evt.run_id == run.id
    assert evt.payload["instruction"] == "Simplify the plan"
    assert evt.payload["new_goal_count"] == 2
