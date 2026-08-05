"""Tests for the HorizonX web dashboard (FastAPI + SSE).

Requires: pip install horizonx[dashboard,dev]
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from httpx import ASGITransport, AsyncClient

from horizonx.core.event_bus import Event, InMemoryBus
from horizonx.core.types import (
    AgentConfig,
    Run,
    RunStatus,
    Session,
    SessionStatus,
    Step,
    StepType,
    StrategyConfig,
    Task,
)
from horizonx.dashboard.app import create_app
from horizonx.storage.sqlite import SqliteStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    p = tmp_path / "ws"
    p.mkdir()
    return p


@pytest.fixture
def app(db_path: Path, workspace_root: Path):
    return create_app(db_path=db_path, workspace_root=workspace_root)


@pytest.fixture
async def client(app, db_path: Path, workspace_root: Path) -> AsyncClient:
    # Manually seed app state since ASGI transport doesn't trigger lifespan
    app.state.store = SqliteStore(db_path)
    app.state.bus = InMemoryBus()
    from horizonx.core.runtime import Runtime
    app.state.runtime = Runtime(
        store=app.state.store,
        bus=app.state.bus,
        workspace_root=workspace_root,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def sample_task() -> Task:
    return Task(
        id="dash-test-task",
        name="Dashboard Test",
        description="For dashboard tests",
        prompt="Do nothing",
        strategy=StrategyConfig(kind="single"),
        agent=AgentConfig(type="mock", model="mock"),
    )


@pytest.fixture
async def seeded_run(db_path: Path, workspace_root: Path, sample_task: Task) -> Run:
    store = SqliteStore(db_path)
    ws = workspace_root / "run-001"
    ws.mkdir(exist_ok=True)
    run = Run(id="run-dash-001", task=sample_task, workspace_path=ws, status=RunStatus.COMPLETED)
    await store.save_run(run)
    return run


@pytest.fixture
async def seeded_session(db_path: Path, seeded_run: Run) -> Session:
    store = SqliteStore(db_path)
    sess = Session(
        id="sess-dash-001",
        run_id=seeded_run.id,
        sequence_index=0,
        status=SessionStatus.COMPLETED,
        steps_count=3,
        tokens_used=1200,
    )
    await store.save_session(sess)
    return sess


@pytest.fixture
async def seeded_steps(db_path: Path, seeded_session: Session) -> list[Step]:
    store = SqliteStore(db_path)
    steps = [
        Step(
            id=f"step-{i}",
            session_id=seeded_session.id,
            sequence=i,
            type=StepType.THOUGHT,
            content={"text": f"step content {i}"},
        )
        for i in range(3)
    ]
    for s in steps:
        await store.save_step(s)
    return steps


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health(client: AsyncClient) -> None:
    r = await client.get("/api/health")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_runs_empty(client: AsyncClient) -> None:
    r = await client.get("/api/runs")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_list_runs_with_data(client: AsyncClient, seeded_run: Run) -> None:
    r = await client.get("/api/runs")
    assert r.status_code == 200
    runs = r.json()
    assert len(runs) == 1
    assert runs[0]["id"] == seeded_run.id
    assert runs[0]["task_name"] == "Dashboard Test"
    assert runs[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_get_run(client: AsyncClient, seeded_run: Run) -> None:
    r = await client.get(f"/api/runs/{seeded_run.id}")
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == seeded_run.id


@pytest.mark.asyncio
async def test_get_run_not_found(client: AsyncClient) -> None:
    r = await client.get("/api/runs/nonexistent-run")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_list_runs_status_filter(client: AsyncClient, seeded_run: Run) -> None:
    r_running = await client.get("/api/runs?status=running")
    assert r_running.json() == []
    r_done = await client.get("/api/runs?status=completed")
    assert len(r_done.json()) == 1


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_sessions(client: AsyncClient, seeded_session: Session, seeded_run: Run) -> None:
    r = await client.get(f"/api/runs/{seeded_run.id}/sessions")
    assert r.status_code == 200
    sessions = r.json()
    assert len(sessions) == 1
    assert sessions[0]["id"] == seeded_session.id
    assert sessions[0]["sequence_index"] == 0


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_steps(
    client: AsyncClient, seeded_steps: list[Step], seeded_run: Run, seeded_session: Session
) -> None:
    r = await client.get(
        f"/api/runs/{seeded_run.id}/sessions/{seeded_session.id}/steps"
    )
    assert r.status_code == 200
    steps = r.json()
    assert len(steps) == 3
    assert steps[0]["sequence"] == 0


@pytest.mark.asyncio
async def test_list_steps_pagination(
    client: AsyncClient, seeded_steps: list[Step], seeded_run: Run, seeded_session: Session
) -> None:
    r = await client.get(
        f"/api/runs/{seeded_run.id}/sessions/{seeded_session.id}/steps?after=0"
    )
    assert r.status_code == 200
    steps = r.json()
    # after=0 means sequence > 0, so 2 steps
    assert len(steps) == 2
    assert all(s["sequence"] > 0 for s in steps)


# ---------------------------------------------------------------------------
# HITL — file-drop resolution
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hitl_resolve_writes_file(
    client: AsyncClient, app, seeded_run: Run, tmp_path: Path
) -> None:
    store = SqliteStore(tmp_path / "test.db")
    request_id = await store.save_hitl_event(
        seeded_run.id, "validator_paused", {}
    )
    r = await client.post(
        f"/api/runs/{seeded_run.id}/hitl",
        json={
            "action": "approve",
            "instruction": "",
            "operator": "test",
            "request_id": request_id,
            "idempotency_key": "dashboard-approve-once",
        },
    )
    assert r.status_code == 200
    written_path = Path(r.json()["path"])
    assert written_path.exists()
    decision = json.loads(written_path.read_text())
    assert decision["action"] == "approve"
    assert decision["operator"] == "test"
    duplicate = await client.post(
        f"/api/runs/{seeded_run.id}/hitl",
        json={
            "action": "approve",
            "instruction": "",
            "operator": "test",
            "request_id": request_id,
            "idempotency_key": "dashboard-approve-once",
        },
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "duplicate"
    events = await store.list_events(seeded_run.id, event_type="hitl.resolved")
    assert len(events) == 1
    assert isinstance(events[0].sequence, int)
    from horizonx.dashboard.routes_events import _event_gen

    replay = _event_gen(
        app.state.bus,
        store=store,
        run_id=seeded_run.id,
        after_sequence=0,
    )
    replayed = await anext(replay)
    assert replayed["id"] == str(events[0].sequence)
    await replay.aclose()
    await store.close()


@pytest.mark.asyncio
async def test_hitl_resolve_not_found(client: AsyncClient) -> None:
    r = await client.post(
        "/api/runs/nonexistent/hitl",
        json={"action": "approve"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_hitl_resolve_requires_pending_request(
    client: AsyncClient, seeded_run: Run
) -> None:
    response = await client.post(
        f"/api/runs/{seeded_run.id}/hitl",
        json={"action": "approve", "operator": "test"},
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_hitl_resolve_rejects_request_from_another_run(
    client: AsyncClient, app, seeded_run: Run
) -> None:
    other = seeded_run.model_copy(update={"id": "run-other"})
    await app.state.store.save_run(other)
    other_request = await app.state.store.save_hitl_event(
        other.id, "validator_paused", {}
    )
    response = await client.post(
        f"/api/runs/{seeded_run.id}/hitl",
        json={"action": "approve", "request_id": other_request},
    )
    assert response.status_code == 404
    assert (await app.state.store.find_hitl_event(other_request))["resolved_at"] is None


# ---------------------------------------------------------------------------
# Launch (POST /api/runs)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_launch_valid_task(client: AsyncClient, sample_task: Task) -> None:
    task_dict = sample_task.model_dump(mode="json")
    task_dict["id"] = "launch-test-001"  # unique id

    r = await client.post("/api/runs", json={"task": task_dict})
    assert r.status_code == 202
    data = r.json()
    assert "run_id" in data
    assert data["run_id"].startswith("run-")


@pytest.mark.asyncio
async def test_launch_invalid_task_returns_422(client: AsyncClient) -> None:
    r = await client.post("/api/runs", json={"task": {"id": "bad", "prompt": "x"}})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# SSE events
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sse_event_gen_yields_bus_events() -> None:
    """_event_gen async generator yields SSE dicts for matching bus events."""
    from horizonx.dashboard.routes_events import _event_gen

    bus = InMemoryBus()
    received: list[dict] = []

    async def collect() -> None:
        async for item in _event_gen(bus, predicate=lambda e: e.run_id == "sse-run"):
            received.append(item)
            return

    task = asyncio.create_task(collect())
    await asyncio.sleep(0.01)
    await bus.publish(Event(type="run.started", run_id="sse-run"))
    await asyncio.wait_for(task, timeout=2.0)

    assert len(received) == 1
    assert received[0]["event"] == "run.started"
    assert "run.started" in received[0]["data"]


@pytest.mark.asyncio
async def test_bus_delivers_events_with_predicate() -> None:
    """InMemoryBus delivers only events matching the predicate."""
    bus = InMemoryBus()
    received: list[Event] = []

    async def collect() -> None:
        async for event in bus.subscribe(predicate=lambda e: e.run_id == "target"):
            received.append(event)
            return

    task = asyncio.create_task(collect())
    await asyncio.sleep(0.01)
    # Publish non-matching event first
    await bus.publish(Event(type="run.failed", run_id="other"))
    await asyncio.sleep(0.01)
    assert len(received) == 0  # predicate filtered it out
    # Publish matching event
    await bus.publish(Event(type="run.started", run_id="target"))
    await asyncio.wait_for(task, timeout=2.0)

    assert len(received) == 1
    assert received[0].run_id == "target"


@pytest.mark.asyncio
async def test_sse_rejects_invalid_cursor(client: AsyncClient, seeded_run: Run) -> None:
    response = await client.get(
        f"/api/runs/{seeded_run.id}/events?cursor=not-a-sequence"
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_run(client: AsyncClient, app, seeded_run: Run) -> None:
    running = seeded_run.model_copy(
        update={"id": "run-cancellable", "status": RunStatus.RUNNING, "completed_at": None}
    )
    await app.state.store.save_run(running)

    r = await client.post(f"/api/runs/{running.id}/cancel")
    assert r.status_code == 200
    assert r.json()["status"] == "aborted"

    # Verify status updated in DB
    running.status = RunStatus.COMPLETED
    await app.state.store.save_run(running)
    r2 = await client.get(f"/api/runs/{running.id}")
    assert r2.json()["status"] == "aborted"
