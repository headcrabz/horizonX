from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

import pytest

from horizonx.core.attempt_executor import AttemptExecutor
from horizonx.core.leases import LeaseManager
from horizonx.core.operator_commands import OperatorCommand, OperatorCommandKind
from horizonx.core.runtime import Runtime
from horizonx.core.types import (
    AgentConfig,
    AttemptStatus,
    Run,
    RunStatus,
    SessionRunResult,
    SessionStatus,
    StrategyConfig,
    Task,
)
from horizonx.hitl.slack_interactions import verify_slack_signature
from horizonx.storage.sqlite import SqliteStore


def _run(tmp_path: Path) -> Run:
    return Run(
        id="run-operator",
        task=Task(
            id="operator-test",
            name="Operator control",
            prompt="wait",
            strategy=StrategyConfig(kind="single"),
            agent=AgentConfig(type="mock", model="mock"),
        ),
        workspace_path=tmp_path,
        status=RunStatus.RUNNING,
    )


@pytest.mark.asyncio
async def test_operator_commands_are_durable_and_idempotent(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "state.db")
    run = _run(tmp_path)
    await store.save_run(run)
    command = OperatorCommand(
        run_id=run.id,
        kind=OperatorCommandKind.CANCEL,
        actor="operator@example.com",
        reason="wrong deployment",
        idempotency_key="cancel-request-1",
    )
    try:
        first, created = await store.create_operator_command(command)
        duplicate, duplicate_created = await store.create_operator_command(
            command.model_copy(update={"id": "different-id"})
        )
        assert created is True
        assert duplicate_created is False
        assert duplicate.id == first.id
        pending = await store.list_operator_commands(run.id, unconsumed_only=True)
        assert pending == [first]
        consumed = await store.consume_operator_command(first.id, attempt_id="attempt-1")
        assert consumed.consumed_at is not None
        assert consumed.attempt_id == "attempt-1"
        assert await store.list_operator_commands(run.id, unconsumed_only=True) == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_hitl_resolution_is_first_writer_wins_and_records_metadata(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "state.db")
    run = _run(tmp_path)
    await store.save_run(run)
    try:
        request_id = await store.save_hitl_event(
            run.id, "validator_paused", {"score": 0.2}, hitl_id="request-1"
        )
        first, resolved = await store.resolve_hitl_event(
            request_id,
            action="modify",
            actor="alice",
            reason="needs safeguards",
            instruction="add a dry run",
            idempotency_key="slack-callback-1",
        )
        duplicate, duplicate_resolved = await store.resolve_hitl_event(
            request_id,
            action="abort",
            actor="mallory",
            reason="duplicate",
            instruction="",
            idempotency_key="slack-callback-1",
        )
        assert resolved is True
        assert duplicate_resolved is False
        assert duplicate == first
        assert first["operator"] == "alice"
        assert first["reason"] == "needs safeguards"
        assert first["instruction"] == "add a dry run"
        assert first["resolved_at"] is not None
    finally:
        await store.close()


def test_slack_signature_rejects_stale_and_invalid_requests() -> None:
    secret = "signing-secret"
    body = b"payload=%7B%7D"
    timestamp = int(time.time())
    base = f"v0:{timestamp}:".encode() + body
    signature = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    verify_slack_signature(
        body=body,
        timestamp=str(timestamp),
        signature=signature,
        signing_secret=secret,
        now=timestamp,
    )
    with pytest.raises(ValueError, match="signature"):
        verify_slack_signature(
            body=body,
            timestamp=str(timestamp),
            signature="v0=bad",
            signing_secret=secret,
            now=timestamp,
        )
    stale_timestamp = timestamp - 301
    stale_base = f"v0:{stale_timestamp}:".encode() + body
    stale_signature = "v0=" + hmac.new(
        secret.encode(), stale_base, hashlib.sha256
    ).hexdigest()
    with pytest.raises(ValueError, match="stale"):
        verify_slack_signature(
            body=body,
            timestamp=str(stale_timestamp),
            signature=stale_signature,
            signing_secret=secret,
            now=timestamp,
        )


class _CommandAwareAgent:
    supports_diagnostic_injection = True

    def __init__(self) -> None:
        self.started = __import__("asyncio").Event()
        self.instructions: list[str] = []

    async def inject_diagnostic(self, diagnostic: str) -> bool:
        self.instructions.append(diagnostic)
        return True

    async def run_session(self, *, cancel_token, **kwargs):  # type: ignore[no-untyped-def]
        import asyncio

        self.started.set()
        while not cancel_token.cancelled:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        return SessionRunResult(
            status=SessionStatus.TIMEOUT,
            error=cancel_token.reason,
        )


@pytest.mark.asyncio
async def test_attempt_consumes_steer_then_cancel_commands(tmp_path: Path) -> None:
    import asyncio

    store = SqliteStore(tmp_path / "state.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    run = _run(tmp_path / "workspace")
    run.workspace_path.mkdir()
    await store.save_run(run)
    agent = _CommandAwareAgent()
    leases = LeaseManager(store)
    lease = await leases.acquire("run:run-operator", owner="test", ttl_seconds=2)
    assert lease is not None
    try:
        with patch("horizonx.core.attempt_executor.build_agent", return_value=agent):
            async def execute_with_lease():  # type: ignore[no-untyped-def]
                async with leases.maintain(lease, ttl_seconds=2):
                    return await AttemptExecutor(runtime).execute(run, prompt="wait")

            execution = asyncio.create_task(execute_with_lease())
            await agent.started.wait()
            await store.create_operator_command(
                OperatorCommand(
                    run_id=run.id,
                    kind=OperatorCommandKind.STEER,
                    actor="alice",
                    instruction="check the lock first",
                    idempotency_key="steer-1",
                )
            )
            await store.create_operator_command(
                OperatorCommand(
                    run_id=run.id,
                    kind=OperatorCommandKind.CANCEL,
                    actor="alice",
                    reason="stop now",
                    idempotency_key="cancel-1",
                )
            )
            result = await asyncio.wait_for(execution, timeout=1)

        assert agent.instructions == ["check the lock first"]
        assert result.attempt.status == AttemptStatus.ABORTED
        commands = await store.list_operator_commands(run.id)
        assert all(command.consumed_at is not None for command in commands)
        assert (await store.load_run(run.id)).status == RunStatus.ABORTED
        assert await store.get_lease("run:run-operator") is None
        run.status = RunStatus.COMPLETED
        await store.save_run(run)
        assert (await store.load_run(run.id)).status == RunStatus.ABORTED
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_runtime_hitl_persists_request_and_resolution(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "state.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    run = _run(tmp_path / "workspace")
    run.workspace_path.mkdir()
    run.task.hitl.require_acknowledgement = True
    await store.save_run(run)
    try:
        decision_task = __import__("asyncio").create_task(
            runtime.request_hitl(
                run, reason="validator_paused", context={"why": "unsafe"}
            )
        )
        for _ in range(50):
            requests = await store.list_hitl_events(run.id)
            if requests:
                break
            await __import__("asyncio").sleep(0.01)
        assert requests[0]["request_actor"] == "system"
        assert requests[0]["request_reason"] == "validator_paused"
        await store.resolve_hitl_event(
            requests[0]["id"],
            action="approve",
            actor="alice",
            reason="reviewed",
            instruction="continue carefully",
            idempotency_key="web-approve-1",
        )
        decision = await __import__("asyncio").wait_for(decision_task, timeout=1)
        assert decision.operator == "alice"
        assert decision.instruction == "continue carefully"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_durable_event_cursor_replays_then_streams(tmp_path: Path) -> None:
    import asyncio

    from horizonx.core.event_bus import Event, InMemoryBus
    from horizonx.dashboard.routes_events import _event_gen

    store = SqliteStore(tmp_path / "state.db")
    bus = InMemoryBus()
    run = _run(tmp_path)
    await store.save_run(run)
    first = await store.append_event(Event(type="run.started", run_id=run.id))
    second = await store.append_event(Event(type="hitl.requested", run_id=run.id))
    stream = _event_gen(bus, store=store, run_id=run.id, after_sequence=first.sequence)
    try:
        replayed = await anext(stream)
        assert replayed["id"] == str(second.sequence)

        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0.01)
        live = await store.append_event(Event(type="hitl.resolved", run_id=run.id))
        await bus.publish(live)
        streamed = await asyncio.wait_for(pending, timeout=1)
        assert streamed["id"] == str(live.sequence)
    finally:
        await stream.aclose()
        await store.close()


@pytest.mark.asyncio
async def test_slack_callback_authenticates_resolves_and_deduplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("httpx")
    from httpx import ASGITransport, AsyncClient

    from horizonx.core.event_bus import InMemoryBus
    from horizonx.dashboard.app import create_app

    app = create_app(tmp_path / "state.db", tmp_path / "workspaces")
    app.state.store = SqliteStore(tmp_path / "state.db")
    app.state.bus = InMemoryBus()
    app.state.runtime = Runtime(
        store=app.state.store,
        bus=app.state.bus,
        workspace_root=tmp_path / "workspaces",
    )
    run = _run(tmp_path)
    await app.state.store.save_run(run)
    request_id = await app.state.store.save_hitl_event(
        run.id, "validator_paused", {}, hitl_id="slack-request"
    )
    payload = {
        "trigger_id": "callback-once",
        "user": {"id": "U123"},
        "actions": [
            {
                "action_id": "hitl_approve",
                "value": f"{request_id}:approve",
            }
        ],
    }
    body = urlencode({"payload": json.dumps(payload)}).encode()
    secret = "test-secret"
    timestamp = str(int(time.time()))
    signature = "v0=" + hmac.new(
        secret.encode(), f"v0:{timestamp}:".encode() + body, hashlib.sha256
    ).hexdigest()
    monkeypatch.setenv("HORIZONX_SLACK_SIGNING_SECRET", secret)
    headers = {
        "content-type": "application/x-www-form-urlencoded",
        "x-slack-request-timestamp": timestamp,
        "x-slack-signature": signature,
    }
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            first = await client.post(
                "/api/hitl/slack/interactions", content=body, headers=headers
            )
            duplicate = await client.post(
                "/api/hitl/slack/interactions", content=body, headers=headers
            )
        assert first.json()["status"] == "resolved"
        assert duplicate.json()["status"] == "duplicate"
        resolved = await app.state.store.find_hitl_event(request_id)
        assert resolved["operator"] == "U123"
        commands = await app.state.store.list_operator_commands(run.id)
        assert len(commands) == 1
    finally:
        await app.state.store.close()


@pytest.mark.asyncio
async def test_runtime_shutdown_cancels_and_awaits_background_runs(
    tmp_path: Path,
) -> None:
    import asyncio

    store = SqliteStore(tmp_path / "state.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    stopped = asyncio.Event()

    async def background() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    runtime.start_background_run("run-one", background(), name="run-one")
    await asyncio.sleep(0)
    await runtime.shutdown()
    assert stopped.is_set()
    assert runtime._background_runs == {}
    with pytest.raises(RuntimeError, match="shutdown"):
        await store.list_pending_runs()


@pytest.mark.asyncio
async def test_authenticated_command_route_persists_steering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("httpx")
    from httpx import ASGITransport, AsyncClient

    from horizonx.core.event_bus import InMemoryBus
    from horizonx.dashboard.app import create_app

    app = create_app(tmp_path / "state.db", tmp_path / "workspaces")
    app.state.store = SqliteStore(tmp_path / "state.db")
    app.state.bus = InMemoryBus()
    app.state.runtime = Runtime(
        store=app.state.store,
        bus=app.state.bus,
        workspace_root=tmp_path / "workspaces",
    )
    run = _run(tmp_path)
    await app.state.store.save_run(run)
    monkeypatch.setenv("HORIZONX_OPERATOR_TOKEN", "operator-secret")
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            denied = await client.post(
                f"/api/runs/{run.id}/commands",
                json={"kind": "steer", "instruction": "inspect the lease"},
            )
            accepted = await client.post(
                f"/api/runs/{run.id}/commands",
                headers={
                    "authorization": "Bearer operator-secret",
                    "idempotency-key": "steer-route-1",
                    "x-horizonx-actor": "alice",
                },
                json={"kind": "steer", "instruction": "inspect the lease"},
            )
        assert denied.status_code == 401
        assert accepted.status_code == 202
        commands = await app.state.store.list_operator_commands(run.id)
        assert [(item.actor, item.instruction) for item in commands] == [
            ("alice", "inspect the lease")
        ]
    finally:
        await app.state.store.close()


@pytest.mark.asyncio
async def test_hitl_policy_skips_disabled_and_unconfigured_triggers(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "state.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    run = _run(tmp_path)
    await store.save_run(run)
    try:
        run.task.hitl.enabled = False
        disabled = await runtime.request_hitl(
            run, reason="validator_paused", context={}
        )
        run.task.hitl.enabled = True
        run.task.hitl.triggers = ["spin_detected"]
        unconfigured = await runtime.request_hitl(
            run, reason="validator_paused", context={}
        )
        assert disabled.action == unconfigured.action == "approve"
        assert await store.list_hitl_events(run.id) == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_hitl_resolution_survives_store_restart(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    first_store = SqliteStore(path)
    run = _run(tmp_path)
    await first_store.save_run(run)
    request_id = await first_store.save_hitl_event(
        run.id, "validator_paused", {"risk": "high"}
    )
    await first_store.resolve_hitl_event(
        request_id,
        action="abort",
        actor="on-call",
        reason="unsafe",
        instruction="roll back",
        idempotency_key="decision-before-crash",
    )
    await first_store.close()

    recovered_store = SqliteStore(path)
    try:
        recovered = await recovered_store.find_hitl_event(request_id)
        assert recovered["decision"] == "abort"
        assert recovered["operator"] == "on-call"
        assert recovered["reason"] == "unsafe"
        assert recovered["instruction"] == "roll back"
    finally:
        await recovered_store.close()


@pytest.mark.asyncio
async def test_acknowledgement_requirement_fails_closed_on_timeout(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "state.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    run = _run(tmp_path)
    run.task.hitl.require_acknowledgement = True
    run.task.hitl.timeout_minutes = 1
    run.task.hitl.escalation_action = "approve"
    await store.save_run(run)
    try:
        monotonic_values = iter((0.0, 61.0))
        with patch("horizonx.hitl.gate.asyncio.sleep", return_value=None):
            with patch("horizonx.hitl.gate.time") as mock_time:
                mock_time.monotonic.side_effect = lambda: next(
                    monotonic_values, 61.0
                )
                decision = await runtime.request_hitl(
                    run, reason="validator_paused", context={}
                )
        assert decision.action == "abort"
        assert decision.operator == "system:timeout"
        assert (await store.load_run(run.id)).status == RunStatus.ABORTED
    finally:
        await store.close()
