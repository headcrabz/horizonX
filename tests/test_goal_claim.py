"""Tests for HX-20: atomic goal claim semantics."""
from __future__ import annotations

import asyncio

import pytest

from horizonx.core.goal_graph import GoalGraph
from horizonx.core.task_board import append_task_board, read_task_board
from horizonx.core.types import GoalNode, GoalStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _seed_goal(store, run_id: str, goal_id: str) -> None:
    """Insert a PENDING goal into the store."""
    g = GoalNode(id=goal_id, name="Test goal", description="A test goal")
    await store.save_goal(run_id, g)


# ---------------------------------------------------------------------------
# claim_goal — basic
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_claim_goal_succeeds_on_pending(store):
    await _seed_goal(store, "run-1", "g.root")
    won = await store.claim_goal("run-1", "g.root", "session-A")
    assert won is True


@pytest.mark.asyncio
async def test_claim_goal_sets_status_in_progress(store):
    await _seed_goal(store, "run-2", "g.root")
    await store.claim_goal("run-2", "g.root", "session-A")
    goals = await store.list_goals("run-2")
    g = next(x for x in goals if x.id == "g.root")
    assert g.status == GoalStatus.IN_PROGRESS
    assert g.assigned_to_session == "session-A"


@pytest.mark.asyncio
async def test_claim_goal_second_call_fails(store):
    await _seed_goal(store, "run-3", "g.root")
    won_a = await store.claim_goal("run-3", "g.root", "session-A")
    won_b = await store.claim_goal("run-3", "g.root", "session-B")
    assert won_a is True
    assert won_b is False


@pytest.mark.asyncio
async def test_claim_nonexistent_goal_fails(store):
    won = await store.claim_goal("run-x", "g.does-not-exist", "session-A")
    assert won is False


# ---------------------------------------------------------------------------
# concurrent claim — only one winner
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_claim_only_one_wins(store):
    """Two coroutines racing on the same goal: exactly one must win."""
    await _seed_goal(store, "run-4", "g.root")

    results = await asyncio.gather(
        store.claim_goal("run-4", "g.root", "session-A"),
        store.claim_goal("run-4", "g.root", "session-B"),
    )
    assert results.count(True) == 1, f"Expected exactly one winner, got {results}"
    assert results.count(False) == 1


@pytest.mark.asyncio
async def test_concurrent_claim_five_racers(store):
    """Five concurrent claimants — exactly one wins."""
    await _seed_goal(store, "run-5", "g.root")

    results = await asyncio.gather(*[
        store.claim_goal("run-5", "g.root", f"session-{i}") for i in range(5)
    ])
    assert results.count(True) == 1
    assert results.count(False) == 4


# ---------------------------------------------------------------------------
# release_goal
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_release_goal_clears_assignment(store):
    await _seed_goal(store, "run-6", "g.root")
    await store.claim_goal("run-6", "g.root", "session-A")
    await store.release_goal("run-6", "g.root")
    goals = await store.list_goals("run-6")
    g = next(x for x in goals if x.id == "g.root")
    assert g.assigned_to_session is None


# ---------------------------------------------------------------------------
# GoalNode.assigned_to_session field
# ---------------------------------------------------------------------------

def test_goal_node_has_assigned_to_session_field():
    g = GoalNode(id="g.root", name="root", description="root goal")
    assert g.assigned_to_session is None


def test_goal_node_assigned_to_session_persists_in_json(tmp_path):
    graph = GoalGraph.empty("Task", "A task")
    graph.root.assigned_to_session = "s.123"
    save_path = tmp_path / "goals.json"
    graph.save(save_path)
    loaded = GoalGraph.load(save_path)
    assert loaded.root.assigned_to_session == "s.123"


# ---------------------------------------------------------------------------
# task_board.jsonl
# ---------------------------------------------------------------------------

def test_append_task_board_creates_file(tmp_path):
    append_task_board(tmp_path, event="claimed", goal_id="g.root",
                      session_id="s.1", agent_type="mock")
    assert (tmp_path / "task_board.jsonl").exists()


def test_append_task_board_multiple_events(tmp_path):
    for event in ("claimed", "completed"):
        append_task_board(tmp_path, event=event, goal_id="g.root",  # type: ignore[arg-type]
                          session_id="s.1", agent_type="mock")
    events = read_task_board(tmp_path)
    assert len(events) == 2
    assert events[0]["event"] == "claimed"
    assert events[1]["event"] == "completed"


def test_read_task_board_last_n(tmp_path):
    for i in range(15):
        append_task_board(tmp_path, event="claimed", goal_id=f"g.node-{i}",
                          session_id=f"s.{i}", agent_type="mock")
    events = read_task_board(tmp_path, last_n=5)
    assert len(events) == 5
    assert events[-1]["goal_id"] == "g.node-14"


def test_read_task_board_empty(tmp_path):
    events = read_task_board(tmp_path)
    assert events == []
