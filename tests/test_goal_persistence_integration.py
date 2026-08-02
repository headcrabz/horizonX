"""Integration invariants for durable, run-scoped goal persistence."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from horizonx.agents.mock import MockAgent
from horizonx.core.goal_graph import GoalGraph
from horizonx.core.runtime import Runtime
from horizonx.core.types import (
    AgentConfig,
    GoalNode,
    GoalStatus,
    RunStatus,
    StrategyConfig,
    Task,
    ValidatorConfig,
)
from horizonx.storage.sqlite import SqliteStore, StoreError


@pytest.mark.asyncio
async def test_goal_identity_is_scoped_to_run(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "goals.db")
    try:
        first = GoalNode(id="g.root", name="First root", description="first")
        second = GoalNode(id="g.root", name="Second root", description="second")

        await store.save_goal("run-one", first)
        await store.save_goal("run-two", second)

        assert (await store.load_goal("run-one", "g.root")).name == "First root"  # type: ignore[union-attr]
        assert (await store.load_goal("run-two", "g.root")).name == "Second root"  # type: ignore[union-attr]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_create_graph_round_trips_nodes_and_edges(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "goals.db")
    try:
        graph = GoalGraph.empty("Root", "root description")
        prerequisite = GoalNode(
            id="g.prerequisite", name="Prerequisite", description="first"
        )
        dependent = GoalNode(
            id="g.dependent",
            name="Dependent",
            description="second",
            depends_on=[prerequisite.id],
        )
        graph.add_child(graph.root.id, prerequisite)
        graph.add_child(graph.root.id, dependent)

        await store.create_graph("run-one", graph)
        loaded = await store.load_graph("run-one")

        assert loaded is not None
        assert {
            node.id: node.model_dump(mode="json") for node in loaded.all_nodes()
        } == {
            node.id: node.model_dump(mode="json") for node in graph.all_nodes()
        }
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_missing_projection_is_regenerated_from_database(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "goals.db")
    projection = tmp_path / "workspace" / "goals.json"
    try:
        graph = GoalGraph.empty("Root", "root description")
        graph.add_child(
            graph.root.id,
            GoalNode(id="g.child", name="Child", description="child description"),
        )
        await store.create_graph("run-one", graph)

        assert await store.ensure_goal_projection("run-one", projection) is True
        assert GoalGraph.load(projection).get("g.child").description == "child description"

        projection.write_text("not-json")
        assert await store.ensure_goal_projection("run-one", projection) is True
        assert GoalGraph.load(projection).get("g.child").description == "child description"
        assert list(projection.parent.glob(".goals.json.*.tmp")) == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_goal_edges_reject_missing_nodes(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "goals.db")
    try:
        child = GoalNode(
            id="g.child",
            parent_id="g.missing",
            name="Child",
            description="child description",
        )
        with pytest.raises(sqlite3.IntegrityError):
            await store.save_goal("run-one", child)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sequential_persists_graph_before_first_claim(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "goals.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    task = Task(
        id="sequential-persistence",
        name="Sequential persistence",
        prompt="Complete the task",
        strategy=StrategyConfig(kind="sequential"),
        agent=AgentConfig(type="mock", model="mock", extra={"steps": []}),
    )
    try:
        agent = MockAgent(steps=[])
        with patch("horizonx.core.attempt_executor.build_agent", return_value=agent):
            run = await asyncio.wait_for(runtime.run(task), timeout=2)
        persisted = await store.load_graph(run.id)

        assert run.status == RunStatus.COMPLETED
        assert persisted is not None
        assert persisted.root.status.value == "done"
        assert persisted.root.attempts == 1
        assert (run.workspace_path / "goals.json").exists()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_decomposition_persists_graph_before_first_claim(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "goals.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    task = Task(
        id="decomposition-persistence",
        name="Decomposition persistence",
        prompt="Complete two ordered steps",
        strategy=StrategyConfig(kind="decomposition"),
        agent=AgentConfig(type="mock", model="mock", extra={"steps": []}),
    )
    decomposition = {
        "subgoals": [
            {
                "name": "First",
                "description": "first step",
                "verification_criteria": ["first done"],
            },
            {
                "name": "Second",
                "description": "second step",
                "verification_criteria": ["second done"],
            },
        ]
    }
    try:
        with patch(
            "horizonx.core.llm_client.call_llm_json",
            new_callable=AsyncMock,
            return_value=decomposition,
        ):
            run = await asyncio.wait_for(runtime.run(task), timeout=2)
        persisted = await store.load_graph(run.id)

        assert run.status == RunStatus.COMPLETED
        assert persisted is not None
        assert persisted.root.status.value == "done"
        assert all(node.attempts == 1 for node in persisted.leaves())
        assert {node.id for node in persisted.all_nodes()} == {
            "g.root",
            "g.sg01",
            "g.sg02",
        }
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_restart_after_graph_creation_can_claim_pending_goal(tmp_path: Path) -> None:
    path = tmp_path / "goals.db"
    first_store = SqliteStore(path)
    graph = GoalGraph.empty("Root", "root description")
    graph.add_child(
        graph.root.id,
        GoalNode(id="g.child", name="Child", description="child description"),
    )
    await first_store.create_graph("run-one", graph)
    await first_store.close()

    restarted_store = SqliteStore(path)
    try:
        loaded = await restarted_store.load_graph("run-one")
        assert loaded is not None
        assert await restarted_store.claim_goal(
            "run-one", "g.child", session_id="session-after-restart"
        )
        claimed = await restarted_store.load_goal("run-one", "g.child")
        assert claimed is not None
        assert claimed.status == GoalStatus.IN_PROGRESS
        assert claimed.assigned_to_session == "session-after-restart"
        assert await restarted_store.integrity_check() == ["ok"]
    finally:
        await restarted_store.close()


@pytest.mark.asyncio
async def test_goal_transition_enforces_version_and_state_machine(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "goals.db")
    try:
        await store.save_goal(
            "run-one", GoalNode(id="g.root", name="Root", description="root")
        )

        with pytest.raises(RuntimeError, match="transition"):
            await store.transition_goal(
                "run-one", "g.root", expected_version=0, to_status=GoalStatus.DONE
            )

        claimed = await store.transition_goal(
            "run-one",
            "g.root",
            expected_version=0,
            to_status=GoalStatus.IN_PROGRESS,
            session_id="sess-one",
        )
        assert claimed.version == 1
        assert claimed.attempts == 1
        assert claimed.assigned_to_session == "sess-one"

        with pytest.raises(RuntimeError, match="version"):
            await store.transition_goal(
                "run-one", "g.root", expected_version=0, to_status=GoalStatus.FAILED
            )

        completed = await store.transition_goal(
            "run-one",
            "g.root",
            expected_version=1,
            to_status=GoalStatus.DONE,
            session_id="sess-one",
        )
        assert completed.version == 2
        assert completed.progress_pct == 100.0
        assert completed.assigned_to_session is None

        with pytest.raises(RuntimeError, match="transition"):
            await store.transition_goal(
                "run-one", "g.root", expected_version=2, to_status=GoalStatus.PENDING
            )

        await store.save_goal(
            "run-one", GoalNode(id="g.optional", name="Optional", description="optional")
        )
        skipped = await store.transition_goal(
            "run-one",
            "g.optional",
            expected_version=0,
            to_status=GoalStatus.SKIPPED,
        )
        assert skipped.status == GoalStatus.SKIPPED
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_every_store_connection_uses_declared_sqlite_policy(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "goals.db", busy_timeout_ms=1234)
    try:
        settings = await store.connection_settings()
        assert settings == {
            "foreign_keys": 1,
            "busy_timeout": 1234,
            "journal_mode": "wal",
            "synchronous": 1,
        }
    finally:
        await store.close()


def test_network_filesystem_database_is_rejected(tmp_path: Path) -> None:
    with patch("horizonx.storage.sqlite._filesystem_type", return_value="nfs"):
        with pytest.raises(StoreError, match="local filesystem"):
            SqliteStore(tmp_path / "goals.db")


def test_in_memory_database_is_rejected() -> None:
    with pytest.raises(StoreError, match="file-backed"):
        SqliteStore(":memory:")


@pytest.mark.asyncio
async def test_lock_timeout_raises_typed_store_error(tmp_path: Path) -> None:
    path = tmp_path / "goals.db"
    store = SqliteStore(path, busy_timeout_ms=25)
    locker = sqlite3.connect(path)
    try:
        locker.execute("BEGIN IMMEDIATE")
        with pytest.raises(RuntimeError, match="busy"):
            await store.save_goal(
                "run-one", GoalNode(id="g.root", name="Root", description="root")
            )
    finally:
        locker.rollback()
        locker.close()
        await store.close()


@pytest.mark.asyncio
async def test_backup_restore_preserves_logical_state_and_integrity(tmp_path: Path) -> None:
    path = tmp_path / "goals.db"
    backup_path = tmp_path / "backups" / "goals.db"
    store = SqliteStore(path)
    try:
        graph = GoalGraph.empty("Original root", "original")
        graph.add_child(
            graph.root.id,
            GoalNode(id="g.child", name="Original child", description="child"),
        )
        await store.create_graph("run-one", graph)
        original_digest = await store.state_digest()

        await store.backup(backup_path)
        graph.get("g.child").name = "Mutated child"
        await store.create_graph("run-one", graph)
        assert await store.state_digest() != original_digest

        await store.restore(backup_path)

        restored = await store.load_graph("run-one")
        assert restored is not None
        assert restored.get("g.child").name == "Original child"
        assert await store.state_digest() == original_digest
        assert await store.integrity_check() == ["ok"]
        checkpoint = await store.checkpoint()
        assert len(checkpoint) == 3
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_backup_and_restore_reject_the_live_database_path(tmp_path: Path) -> None:
    path = tmp_path / "goals.db"
    store = SqliteStore(path)
    try:
        with pytest.raises(StoreError, match="different path"):
            await store.backup(path)
        with pytest.raises(StoreError, match="different path"):
            await store.restore(path)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_create_graph_rolls_back_when_an_edge_is_invalid(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "goals.db")
    try:
        original = GoalGraph.empty("Original", "original")
        await store.create_graph("run-one", original)

        invalid = GoalGraph.empty("Replacement", "replacement")
        invalid.add_child(
            invalid.root.id,
            GoalNode(
                id="g.child",
                name="Child",
                description="child",
                depends_on=["g.missing"],
            ),
        )
        with pytest.raises(sqlite3.IntegrityError):
            await store.create_graph("run-one", invalid)

        persisted = await store.load_graph("run-one")
        assert persisted is not None
        assert persisted.root.name == "Original"
        assert {node.id for node in persisted.all_nodes()} == {"g.root"}
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_replace_pending_subgraph_preserves_completed_nodes(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "goals.db")
    try:
        original = GoalGraph.empty("Root", "root")
        done = GoalNode(
            id="g.done",
            name="Completed work",
            description="must remain immutable",
            status=GoalStatus.DONE,
            progress_pct=100.0,
            version=3,
        )
        original.add_child(original.root.id, done)
        original.add_child(
            original.root.id,
            GoalNode(id="g.old", name="Old pending", description="replace me"),
        )
        await store.create_graph("run-one", original)

        replacement = GoalGraph.empty("Root", "root")
        replacement.add_child(
            replacement.root.id, GoalNode.model_validate(done.model_dump())
        )
        replacement.add_child(
            replacement.root.id,
            GoalNode(id="g.new", name="New pending", description="new plan"),
        )
        await store.replace_pending_subgraph("run-one", replacement)

        persisted = await store.load_graph("run-one")
        assert persisted is not None
        assert persisted.get("g.done").model_dump(mode="json") == done.model_dump(mode="json")
        assert {node.id for node in persisted.all_nodes()} == {"g.root", "g.done", "g.new"}

        changed_done = GoalGraph.empty("Root", "root")
        changed_done.add_child(
            changed_done.root.id,
            GoalNode(
                **{
                    **done.model_dump(),
                    "name": "Rewritten completed work",
                }
            ),
        )
        with pytest.raises(RuntimeError, match="completed goal"):
            await store.replace_pending_subgraph("run-one", changed_done)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_goal_fields_round_trip_without_loss(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "goals.db")
    try:
        await store.save_goal(
            "run-one", GoalNode(id="g.root", name="Root", description="root")
        )
        await store.save_goal(
            "run-one",
            GoalNode(id="g.prerequisite", name="Prerequisite", description="prerequisite"),
        )
        goal = GoalNode(
            id="g.child",
            parent_id="g.root",
            name="Child",
            description="Implement the child",
            verification_criteria=["tests pass", "artifact exists"],
            depends_on=["g.prerequisite"],
            attempts=2,
            max_attempts=7,
            progress_pct=42.5,
            version=9,
            notes="Keep this context",
            assigned_to_session="sess-1",
            validators=[
                ValidatorConfig(
                    id="tests",
                    type="test_suite",
                    runs="final",
                    config={"command": "pytest -q"},
                )
            ],
            inherit_validators=False,
        )

        await store.save_goal("run-one", goal)
        loaded = await store.load_goal("run-one", goal.id)

        assert loaded is not None
        assert loaded.model_dump(mode="json") == goal.model_dump(mode="json")
    finally:
        await store.close()
