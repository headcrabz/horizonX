"""Durable timeline projection and dashboard API contract."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from horizonx.core.event_bus import Event, InMemoryBus
from horizonx.core.goal_graph import GoalGraph
from horizonx.core.operator_commands import OperatorCommand, OperatorCommandKind
from horizonx.core.recovery import RecoveryCoordinator
from horizonx.core.runtime import Runtime
from horizonx.core.types import (
    AgentConfig,
    AttemptRecord,
    AttemptStatus,
    GateAction,
    GateDecision,
    GoalNode,
    Run,
    RunStatus,
    Session,
    SessionStatus,
    SpinReport,
    StrategyConfig,
    Task,
)
from horizonx.dashboard.app import create_app
from horizonx.dashboard.timeline import TimelineProjection
from horizonx.storage.sqlite import SqliteStore, StoreError


def _task() -> Task:
    return Task(
        id="timeline-task", name="Timeline", prompt="record a durable timeline",
        strategy=StrategyConfig(kind="single"), agent=AgentConfig(type="mock", model="mock"),
    )


async def _seed(store: SqliteStore, tmp_path: Path, *, status: RunStatus = RunStatus.RUNNING) -> Run:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    run = Run(id="timeline-run", task=_task(), workspace_path=workspace, status=status)
    await store.save_run(run)
    graph = GoalGraph({"g.root": GoalNode(id="g.root", name="root", description="initial")})
    await store.create_graph(run.id, graph)
    return run


async def _after_initial_graph(store: SqliteStore, run_id: str) -> int:
    snapshot = await store.latest_graph_snapshot(run_id)
    assert snapshot is not None and snapshot["event_sequence"] is not None
    return int(snapshot["event_sequence"])


@pytest.mark.asyncio
async def test_projection_orders_by_sequence_and_hides_raw_payload_after_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "timeline.db"
    store = SqliteStore(db_path)
    run = await _seed(store, tmp_path)
    first = await store.append_event(Event(type="run.started", run_id=run.id, payload={"tool_payload": {"secret": "no"}}))
    second = await store.append_event(Event(type="goal.in_progress", run_id=run.id, goal_id="g.root"))
    await store.close()

    restarted = SqliteStore(db_path)
    page = await TimelineProjection(restarted).page(
        run.id, after=await _after_initial_graph(restarted, run.id), limit=1
    )

    assert [item.sequence for item in page.events] == [first.sequence]
    assert page.next_after == first.sequence
    assert page.events[0].entities.goal_id is None
    assert "payload" not in page.events[0].model_dump()
    assert second.sequence and first.sequence and second.sequence > first.sequence
    await restarted.close()


@pytest.mark.asyncio
async def test_restart_chronology_uses_sequence_not_timezone_timestamps(tmp_path: Path) -> None:
    db_path = tmp_path / "timeline.db"
    store = SqliteStore(db_path)
    run = await _seed(store, tmp_path)
    recovery = await store.append_event(Event(
        type="recovery.planned", run_id=run.id,
        timestamp=datetime(2026, 1, 1, 23, tzinfo=timezone(timedelta(hours=-5))),
    ))
    fork = await store.append_event(Event(
        type="fork.created", run_id=run.id,
        timestamp=datetime(2026, 1, 1, 20, tzinfo=UTC),
    ))
    await store.close()

    restarted = SqliteStore(db_path)
    page = await TimelineProjection(restarted).page(run.id, after=0, limit=10)

    assert [event.type for event in page.events[-2:]] == ["recovery.planned", "fork.created"]
    assert [event.sequence for event in page.events[-2:]] == [recovery.sequence, fork.sequence]
    await restarted.close()


@pytest.mark.asyncio
async def test_real_recovery_and_runtime_fork_events_are_durable(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "timeline.db")
    run = await _seed(store, tmp_path)
    session = Session(id="recovery-session", run_id=run.id, sequence_index=0)
    await store.save_session(session)
    attempt = await store.create_attempt(AttemptRecord(
        run_id=run.id, session_id=session.id, status=AttemptStatus.RUNNING,
        provider="mock", model="mock", workspace_path=run.workspace_path,
    ))
    await RecoveryCoordinator(store).plan(owner="timeline-recovery")
    runtime = Runtime(store=store, workspace_root=tmp_path / "forks")
    fork = await runtime.fork_run(run.id)

    recovery = (await store.list_events(run.id, event_type="recovery.planned"))[-1]
    fork_event = (await store.list_events(fork.id, event_type="fork.created"))[-1]
    page = await TimelineProjection(store).page(fork.id, after=0, limit=10)

    assert recovery.attempt_id == attempt.id
    assert fork_event.payload["parent_run_id"] == run.id
    assert page.events[-1].entities.run_id == fork.id
    await store.close()


@pytest.mark.asyncio
async def test_projection_playback_uses_recorded_graph_snapshot_and_digest(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "timeline.db")
    run = await _seed(store, tmp_path)
    before = await store.latest_graph_snapshot(run.id)
    changed = GoalGraph({
        "g.root": GoalNode(id="g.root", name="root", description="initial", children=["g.child"]),
        "g.child": GoalNode(id="g.child", parent_id="g.root", name="child", description="recorded"),
    })
    event = await store.replace_pending_subgraph_and_append_event(
        run.id, changed, Event(type="goals.re_decomposed", run_id=run.id, payload={"instruction": "split"})
    )

    playback = await TimelineProjection(store).playback(run.id, sequence=event.sequence or 0)

    assert playback.graph is not None
    assert playback.graph_digest == event.payload["graph_after_digest"]
    assert playback.graph_digest != before["digest"]
    assert playback.graph["nodes"]["g.child"]["description"] == "recorded"
    assert event.payload["graph_before_version"] < event.payload["graph_after_version"]
    await store.close()


@pytest.mark.asyncio
async def test_initial_graph_event_is_bound_after_pre_graph_run_event(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "timeline.db")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run = Run(id="ordered-run", task=_task(), workspace_path=workspace, status=RunStatus.RUNNING)
    await store.save_run(run)
    started = await store.append_event(Event(type="run.started", run_id=run.id))
    graph = GoalGraph({"g.root": GoalNode(id="g.root", name="root", description="initial")})
    await store.create_graph(run.id, graph)
    [graph_event] = await store.list_events(run.id, after_sequence=started.sequence)

    projection = TimelineProjection(store)
    before = await projection.playback(run.id, sequence=started.sequence or 0)
    after = await projection.playback(run.id, sequence=graph_event.sequence or 0)

    assert graph_event.type == "goals.graph_changed"
    assert graph_event.payload["graph_before_digest"] is None
    assert before.graph is None
    assert after.graph and after.graph["nodes"]["g.root"]["status"] == "pending"
    await store.close()


@pytest.mark.asyncio
async def test_unpaired_durable_graph_mutation_is_rejected_before_redecomposition(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "timeline.db")
    run = await _seed(store, tmp_path)
    with store._conn() as connection:
        connection.execute(
            "UPDATE goals SET status='in_progress', version=version+1 "
            "WHERE run_id=? AND id='g.root'",
            (run.id,),
        )
    current = await store.load_graph(run.id)
    assert current is not None

    with pytest.raises(StoreError, match="latest durable snapshot"):
        await store.replace_pending_subgraph_and_append_event(
            run.id, current, Event(type="goals.re_decomposed", run_id=run.id)
        )

    await store.close()


@pytest.mark.asyncio
async def test_public_save_goal_creates_initial_graph_event_and_snapshot(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "timeline.db")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run = Run(id="save-goal-run", task=_task(), workspace_path=workspace, status=RunStatus.RUNNING)
    await store.save_run(run)

    await store.save_goal(
        run.id, GoalNode(id="g.root", name="root", description="saved publicly")
    )

    [event] = await store.list_events(run.id, event_type="goals.graph_changed")
    snapshot = await store.latest_graph_snapshot(run.id)
    playback = await TimelineProjection(store).playback(run.id, sequence=event.sequence or 0)

    assert snapshot is not None and snapshot["event_sequence"] == event.sequence
    assert playback.graph and playback.graph["nodes"]["g.root"]["description"] == "saved publicly"
    await store.close()


@pytest.mark.asyncio
async def test_redecomposition_rejects_existing_graph_without_snapshot_baseline(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "timeline.db")
    run = await _seed(store, tmp_path)
    with store._conn() as connection:
        connection.execute("DELETE FROM graph_snapshots WHERE run_id=?", (run.id,))
    graph = await store.load_graph(run.id)
    assert graph is not None

    with pytest.raises(StoreError, match="no durable snapshot baseline"):
        await store.replace_pending_subgraph_and_append_event(
            run.id, graph, Event(type="goals.re_decomposed", run_id=run.id)
        )

    await store.close()


@pytest.mark.asyncio
async def test_playback_ignores_unbound_legacy_snapshot_before_any_graph_event(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "timeline.db")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run = Run(id="legacy-snapshot-run", task=_task(), workspace_path=workspace, status=RunStatus.RUNNING)
    await store.save_run(run)
    started = await store.append_event(Event(type="run.started", run_id=run.id))
    legacy_graph = GoalGraph({"g.root": GoalNode(id="g.root", name="root", description="legacy")})
    snapshot, digest = store._graph_snapshot(legacy_graph)
    with store._conn() as connection:
        connection.execute(
            "INSERT INTO graph_snapshots "
            "(run_id, version, digest, snapshot, event_sequence, recorded_at) "
            "VALUES (?, 1, ?, ?, NULL, ?)",
            (run.id, digest, snapshot, "2026-01-01T00:00:00+00:00"),
        )

    projection = TimelineProjection(store)
    assert (await projection.playback(run.id, sequence=0)).graph is None
    assert (await projection.playback(run.id, sequence=started.sequence or 0)).graph is None
    await store.close()


@pytest.mark.asyncio
async def test_noop_graph_writes_do_not_emit_fake_transitions(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "timeline.db")
    run = await _seed(store, tmp_path)
    graph = await store.load_graph(run.id)
    assert graph is not None
    before = await store.list_events(run.id)

    await store.save_goal(run.id, graph.root)
    await store.replace_pending_subgraph(run.id, graph)

    assert await store.list_events(run.id) == before
    assert await store.replace_pending_subgraph_and_append_event(
        run.id, graph, Event(type="goals.re_decomposed", run_id=run.id)
    ) is None
    await store.close()


@pytest.mark.asyncio
async def test_playback_preserves_each_ordinary_goal_transition(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "timeline.db")
    run = await _seed(store, tmp_path)
    in_progress = await store.load_graph(run.id)
    assert in_progress is not None
    in_progress.mark_in_progress("g.root", by_session="session-1")
    await store.create_graph(run.id, in_progress)
    started = await store.append_event(
        Event(type="goal.in_progress", run_id=run.id, payload={"goal_id": "g.root"})
    )
    completed = await store.load_graph(run.id)
    assert completed is not None
    completed.mark_done("g.root", by_session="session-1")
    await store.create_graph(run.id, completed)
    done = await store.append_event(
        Event(type="goal.done", run_id=run.id, payload={"goal_id": "g.root"})
    )

    projection = TimelineProjection(store)
    at_started = await projection.playback(run.id, sequence=started.sequence or 0)
    at_done = await projection.playback(run.id, sequence=done.sequence or 0)

    assert at_started.graph and at_started.graph["nodes"]["g.root"]["status"] == "in_progress"
    assert at_done.graph and at_done.graph["nodes"]["g.root"]["status"] == "done"
    assert at_started.graph_digest != at_done.graph_digest
    page = await projection.page(run.id, after=0, limit=10)
    assert next(item for item in page.events if item.sequence == started.sequence).entities.goal_id == "g.root"
    await store.close()


@pytest.mark.asyncio
async def test_reverted_graph_digest_is_recorded_for_each_transition(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "timeline.db")
    run = await _seed(store, tmp_path)
    initial = await store.latest_graph_snapshot(run.id)
    assert initial is not None
    original = await store.load_graph(run.id)
    assert original is not None
    expanded = GoalGraph({
        "g.root": GoalNode(id="g.root", name="root", description="initial", children=["g.child"]),
        "g.child": GoalNode(id="g.child", parent_id="g.root", name="child", description="child"),
    })
    first = await store.replace_pending_subgraph_and_append_event(
        run.id, expanded, Event(type="goals.re_decomposed", run_id=run.id)
    )
    second = await store.replace_pending_subgraph_and_append_event(
        run.id, original, Event(type="goals.re_decomposed", run_id=run.id)
    )

    replayed = await TimelineProjection(store).playback(run.id, sequence=second.sequence or 0)

    assert first.payload["graph_after_digest"] != second.payload["graph_after_digest"]
    assert second.payload["graph_after_digest"] == initial["digest"]
    assert second.payload["graph_after_version"] > first.payload["graph_after_version"]
    assert replayed.graph_digest == initial["digest"]
    await store.close()


@pytest.mark.asyncio
async def test_graph_snapshot_migration_removes_legacy_digest_uniqueness(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-timeline.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE graph_snapshots (run_id TEXT NOT NULL, version INTEGER NOT NULL, "
            "digest TEXT NOT NULL, snapshot TEXT NOT NULL, event_sequence INTEGER, "
            "recorded_at TEXT NOT NULL, PRIMARY KEY (run_id, version), UNIQUE (run_id, digest))"
        )
        conn.execute(
            "CREATE INDEX idx_graph_snapshots_playback "
            "ON graph_snapshots(run_id, event_sequence, version)"
        )
    store = SqliteStore(db_path)
    with sqlite3.connect(db_path) as conn:
        indexes = conn.execute("PRAGMA index_list(graph_snapshots)").fetchall()
        unique_sets = {
            tuple(column[2] for column in conn.execute(f"PRAGMA index_info('{index[1]}')"))
            for index in indexes if index[2]
        }
    assert ("run_id", "digest") not in unique_sets
    assert "idx_graph_snapshots_playback" in {index[1] for index in indexes}
    await store.close()


@pytest.mark.asyncio
async def test_projection_detail_and_entity_links_are_explicit(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "timeline.db")
    run = await _seed(store, tmp_path)
    session = Session(
        id="session-1", run_id=run.id, sequence_index=0,
        target_goal_id="g.root", status=SessionStatus.COMPLETED,
    )
    await store.save_session(session)
    validation_id = await store.save_validation(
        run,
        session,
        GateDecision(
            decision=GateAction.ABORT, reason="failed", validator_name="real-validator"
        ),
    )
    spin_report_id = await store.save_spin_report(
        session,
        SpinReport(detected=True, layer="real-spin", action="terminate_and_hitl"),
    )
    event = await store.append_event(Event(
        type="validator.failed", run_id=run.id, goal_id="g.root", session_id="session-1",
        payload={
            "validation_id": validation_id, "tool_payload": {"large": True},
        },
    ))
    await store.append_event(Event(
        type="spin.detected", run_id=run.id, session_id=session.id,
        payload={"spin_report_id": spin_report_id},
    ))
    projection = TimelineProjection(store)

    page = await projection.page(run.id, after=0, limit=10)
    detail = await projection.event_detail(run.id, event.sequence or 0)

    assert next(item for item in page.events if item.sequence == event.sequence).entities.validation_id == validation_id
    assert next(item for item in page.events if item.type == "spin.detected").entities.spin_report_id == spin_report_id
    assert "tool_payload" not in page.events[0].model_dump_json()
    assert detail.payload["tool_payload"] == {"large": True}
    await store.close()


@pytest.mark.asyncio
async def test_projection_is_safe_for_empty_failed_runs(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "timeline.db")
    run = await _seed(store, tmp_path, status=RunStatus.FAILED)

    page = await TimelineProjection(store).page(run.id, after=0, limit=10)
    playback = await TimelineProjection(store).playback(run.id, sequence=0)

    assert page.run_status == "failed"
    assert page.events[0].type == "goals.graph_changed"
    assert playback.graph is None
    await store.close()


@pytest.mark.asyncio
async def test_timeline_api_paginates_details_and_reports_safe_errors(tmp_path: Path) -> None:
    db_path = tmp_path / "timeline.db"
    workspace = tmp_path / "workspace-root"
    workspace.mkdir()
    store = SqliteStore(db_path)
    run = await _seed(store, tmp_path, status=RunStatus.RUNNING)
    first = await store.append_event(Event(type="recovery.planned", run_id=run.id, payload={"raw": "hidden"}))
    await store.append_event(Event(type="fork.created", run_id=run.id))
    await store.close()
    app = create_app(db_path=db_path, workspace_root=workspace)
    app.state.store = SqliteStore(db_path)
    app.state.bus = InMemoryBus()
    app.state.runtime = Runtime(store=app.state.store, bus=app.state.bus, workspace_root=workspace)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        page = await client.get(f"/api/runs/{run.id}/timeline?after=0&limit=1")
        detail = await client.get(f"/api/runs/{run.id}/timeline/{first.sequence}")
        playback = await client.get(f"/api/runs/{run.id}/timeline/playback?sequence={first.sequence}")
        missing = await client.get("/api/runs/missing/timeline")
        invalid = await client.get(f"/api/runs/{run.id}/timeline?after=-1")
        invalid_limit = await client.get(f"/api/runs/{run.id}/timeline?limit=501")

    assert page.status_code == 200
    assert page.json()["run_status"] == "running"
    assert len(page.json()["events"]) == 1
    assert "payload" not in page.json()["events"][0]
    assert detail.json()["payload"] == {"raw": "hidden"}
    assert playback.status_code == 200
    assert missing.status_code == 404
    assert invalid.status_code == 422
    assert invalid_limit.status_code == 422
    await app.state.store.close()


@pytest.mark.asyncio
async def test_timeline_next_after_requires_an_additional_event(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "timeline.db")
    run = await _seed(store, tmp_path)
    for event_type in ("run.started", "attempt.started"):
        await store.append_event(Event(type=event_type, run_id=run.id))

    after = await _after_initial_graph(store, run.id)
    exact = await TimelineProjection(store).page(run.id, after=after, limit=2)
    partial = await TimelineProjection(store).page(run.id, after=after, limit=1)

    assert exact.next_after is None
    assert partial.next_after == partial.events[0].sequence
    await store.close()


@pytest.mark.asyncio
async def test_timeline_page_exposes_durable_high_water_beyond_current_page(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "timeline.db")
    run = Run(
        id="high-water-run", task=_task(), workspace_path=tmp_path / "high-water", status=RunStatus.RUNNING
    )
    await store.save_run(run)
    for _ in range(1_000):
        await store.append_event(Event(type="step.recorded", run_id=run.id))

    page = await TimelineProjection(store).page(run.id, after=0, limit=100)
    empty = await TimelineProjection(store).page(run.id, after=0, limit=100)
    empty_run = Run(
        id="empty-timeline-run", task=_task(), workspace_path=tmp_path / "empty", status=RunStatus.RUNNING
    )
    await store.save_run(empty_run)
    empty_page = await TimelineProjection(store).page(empty_run.id, after=0, limit=100)

    assert page.next_after == 100
    assert page.latest_sequence == 1_000
    assert empty.latest_sequence == 1_000
    assert empty_page.latest_sequence == 0
    await store.close()


@pytest.mark.asyncio
async def test_atomic_timeline_page_high_water_is_a_safe_sse_boundary(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "timeline.db")
    run = Run(
        id="atomic-high-water-run", task=_task(), workspace_path=tmp_path / "atomic", status=RunStatus.RUNNING
    )
    await store.save_run(run)
    for _ in range(100):
        await store.append_event(Event(type="step.recorded", run_id=run.id))

    rows, high_water = await store.list_event_summaries_with_high_water(
        run.id, after_sequence=0, limit=100
    )
    appended = await store.append_event(Event(type="step.recorded", run_id=run.id))
    after_boundary = await store.list_event_summaries(
        run.id, after_sequence=high_water, limit=100
    )

    assert len(rows) == 100
    assert high_water == 100
    assert rows[-1]["sequence"] == high_water
    assert [row["sequence"] for row in after_boundary] == [appended.sequence]
    await store.close()


@pytest.mark.asyncio
async def test_real_hitl_resolution_and_operator_command_are_linked(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "timeline.db")
    run = await _seed(store, tmp_path)
    request_id, _ = await store.enter_hitl(run.id, "manual", {"goal_id": "g.root"})
    command = OperatorCommand(
        run_id=run.id,
        kind=OperatorCommandKind.CANCEL,
        actor="operator",
        idempotency_key="timeline-cancel",
    )
    await store.submit_cancel_command(command)

    page = await TimelineProjection(store).page(run.id, after=0, limit=10)
    resolved = next(event for event in page.events if event.type == "hitl.resolved")

    assert resolved.entities.hitl_id == request_id
    assert resolved.entities.command_id == command.id
    await store.close()


@pytest.mark.asyncio
async def test_primary_active_hitl_decision_includes_command_link(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "timeline.db")
    run = await _seed(store, tmp_path)
    request_id, _ = await store.enter_hitl(run.id, "manual", {})
    command = OperatorCommand(
        run_id=run.id,
        kind=OperatorCommandKind.DECISION,
        actor="operator",
        payload={"request_id": request_id, "action": "approve"},
        idempotency_key="timeline-decision",
    )
    outcome = await store.submit_active_hitl_decision(command)

    detail = await TimelineProjection(store).event_detail(run.id, outcome.event.sequence or 0)

    assert detail.payload["command_id"] == command.id
    assert detail.entities.command_id == command.id
    await store.close()
