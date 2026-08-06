from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

import pytest

from horizonx.agents.mock import MockAgent
from horizonx.core.attempt_executor import AttemptExecutor
from horizonx.core.leases import LeaseManager
from horizonx.core.operator_commands import OperatorCommand, OperatorCommandKind
from horizonx.core.runtime import Runtime
from horizonx.core.types import (
    AgentConfig,
    AttemptRecord,
    AttemptStatus,
    Run,
    RunStatus,
    Session,
    SessionRunResult,
    SessionStatus,
    StrategyConfig,
    Task,
)
from horizonx.hitl.slack_interactions import verify_slack_signature
from horizonx.storage.sqlite import (
    HITLTransitionError,
    OperatorCommandConflict,
    SqliteStore,
)


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
async def test_hitl_entry_owns_exact_request_and_rejects_cross_run_id(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "state.db")
    first = _run(tmp_path).model_copy(update={"id": "run-first"})
    second = _run(tmp_path).model_copy(update={"id": "run-second"})
    await store.save_run(first)
    await store.save_run(second)
    try:
        request_id, requested = await store.enter_hitl(
            first.id, "validator_paused", {"generation": 1}, hitl_id="shared-id"
        )
        paused = await store.load_run(first.id)
        assert paused.status == RunStatus.PAUSED_HITL
        assert paused.active_hitl_request_id == request_id
        assert requested.id == f"hitl.requested:{request_id}"

        with pytest.raises(HITLTransitionError, match="request"):
            await store.enter_hitl(
                second.id,
                "validator_paused",
                {"generation": 2},
                hitl_id="shared-id",
            )
        untouched = await store.load_run(second.id)
        assert untouched.status == RunStatus.RUNNING
        assert untouched.active_hitl_request_id is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_resolution_retains_owner_until_fenced_consumption_clears_once(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "state.db")
    run = _run(tmp_path)
    await store.save_run(run)
    request_id, _ = await store.enter_hitl(
        run.id, "validator_paused", {"generation": 1}
    )
    await store.resolve_hitl_event_and_event(
        request_id,
        action="approve",
        actor="alice",
        reason="safe",
        instruction="continue",
        idempotency_key="approve-generation-1",
    )
    try:
        resolved_but_active = await store.load_run(run.id)
        assert resolved_but_active.status == RunStatus.PAUSED_HITL
        assert resolved_but_active.active_hitl_request_id == request_id

        resumed = await store.apply_hitl_decision(
            run.id, expected_request_id=request_id, to_status=RunStatus.RUNNING
        )
        assert resumed.status == RunStatus.RUNNING
        assert resumed.active_hitl_request_id is None
        with pytest.raises(HITLTransitionError, match="active HITL request"):
            await store.apply_hitl_decision(
                run.id, expected_request_id=request_id, to_status=RunStatus.RUNNING
            )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_terminal_winner_fences_late_hitl_consumption(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "state.db")
    run = _run(tmp_path)
    await store.save_run(run)
    request_id, _ = await store.enter_hitl(run.id, "validator_paused", {})
    await store.resolve_hitl_event_and_event(
        request_id,
        action="approve",
        actor="alice",
        reason="safe",
        instruction="",
        idempotency_key="late-approval",
    )
    await store.transition_run(run.id, RunStatus.ABORTED)
    try:
        with pytest.raises(HITLTransitionError):
            await store.apply_hitl_decision(
                run.id, expected_request_id=request_id,
                to_status=RunStatus.COMPLETED,
            )
        winner = await store.load_run(run.id)
        assert winner.status == RunStatus.ABORTED
        assert winner.active_hitl_request_id is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_active_request_lookup_rejects_prior_generation(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "state.db")
    run = _run(tmp_path)
    await store.save_run(run)
    first_id, _ = await store.enter_hitl(run.id, "validator_paused", {})
    await store.resolve_hitl_event_and_event(
        first_id,
        action="approve",
        actor="alice",
        reason="safe",
        instruction="",
        idempotency_key="lookup-first",
    )
    try:
        assert (await store.find_active_hitl_event(run.id, first_id))["id"] == first_id
        await store.apply_hitl_decision(
            run.id,
            expected_request_id=first_id,
            to_status=RunStatus.RUNNING,
        )
        second_id, _ = await store.enter_hitl(run.id, "validator_paused", {})
        with pytest.raises(HITLTransitionError):
            await store.find_active_hitl_event(run.id, first_id)
        assert (await store.find_active_hitl_event(run.id, second_id))["id"] == second_id
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_two_store_active_decision_race_has_one_command_and_exact_replay(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    first_store = SqliteStore(path)
    second_store = SqliteStore(path)
    run = _run(tmp_path)
    await first_store.save_run(run)
    request_id, _ = await first_store.enter_hitl(run.id, "validator_paused", {})
    approve = OperatorCommand(
        run_id=run.id,
        kind=OperatorCommandKind.DECISION,
        actor="alice",
        reason="approve reason",
        instruction="ship it",
        payload={"request_id": request_id, "action": "approve"},
        idempotency_key="race-approve",
    )
    abort = OperatorCommand(
        run_id=run.id,
        kind=OperatorCommandKind.DECISION,
        actor="bob",
        reason="abort reason",
        instruction="stop it",
        payload={"request_id": request_id, "action": "abort"},
        idempotency_key="race-abort",
    )
    start = asyncio.Event()

    async def submit(store: SqliteStore, command: OperatorCommand) -> object:
        await start.wait()
        try:
            return await store.submit_active_hitl_decision(command)
        except OperatorCommandConflict as exc:
            return exc

    first = asyncio.create_task(submit(first_store, approve))
    second = asyncio.create_task(submit(second_store, abort))
    start.set()
    outcomes = await asyncio.gather(first, second)
    try:
        winners = [item for item in outcomes if not isinstance(item, Exception)]
        conflicts = [item for item in outcomes if isinstance(item, Exception)]
        assert len(winners) == 1
        assert len(conflicts) == 1
        winner = winners[0]
        assert winner.created is True  # type: ignore[attr-defined]

        [stored_command] = await first_store.list_operator_commands(run.id)
        [resolved] = await first_store.list_hitl_events(run.id)
        [event] = await first_store.list_events(run.id, event_type="hitl.resolved")
        assert stored_command.id == winner.command.id  # type: ignore[attr-defined]
        assert resolved["decision"] == stored_command.payload["action"]
        assert resolved["resolution_idempotency_key"] == stored_command.idempotency_key
        assert event.id == f"hitl-resolved:{request_id}"

        replay = await second_store.submit_active_hitl_decision(
            stored_command.model_copy(update={"id": "replay-command-id"})
        )
        assert replay.created is False
        assert replay.command.id == stored_command.id
        assert replay.request["decision"] == resolved["decision"]

        conflicting_replays = [
            stored_command.model_copy(update={"actor": "different-actor"}),
            stored_command.model_copy(update={"reason": "different-reason"}),
            stored_command.model_copy(update={"instruction": "different-instruction"}),
            stored_command.model_copy(
                update={"payload": {**stored_command.payload, "extra": True}}
            ),
            stored_command.model_copy(
                update={
                    "payload": {
                        "request_id": request_id,
                        "action": (
                            "abort"
                            if stored_command.payload["action"] == "approve"
                            else "approve"
                        ),
                    }
                }
            ),
            stored_command.model_copy(update={"idempotency_key": "different-key"}),
        ]
        for conflicting in conflicting_replays:
            with pytest.raises(OperatorCommandConflict):
                await second_store.submit_active_hitl_decision(conflicting)
        assert len(await first_store.list_operator_commands(run.id)) == 1
    finally:
        await first_store.close()
        await second_store.close()


@pytest.mark.asyncio
async def test_authoritative_active_decision_lookup_is_strict(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    store = SqliteStore(path)
    run = _run(tmp_path)
    await store.save_run(run)
    request_id, _ = await store.enter_hitl(run.id, "validator_paused", {})
    try:
        with pytest.raises(OperatorCommandConflict, match="not resolved"):
            await store.load_authoritative_active_hitl_decision(run.id, request_id)

        submitted = await store.submit_active_hitl_decision(
            OperatorCommand(
                run_id=run.id,
                kind=OperatorCommandKind.DECISION,
                actor="human-operator",
                reason="human reason",
                instruction="human instruction",
                payload={"request_id": request_id, "action": "modify"},
                idempotency_key="human-authoritative",
            )
        )
        loaded = await store.load_authoritative_active_hitl_decision(
            run.id, request_id
        )
        assert loaded.created is False
        assert loaded.command == submitted.command
        assert loaded.request == submitted.request
        assert loaded.event == submitted.event

        with sqlite3.connect(path) as connection:
            connection.execute(
                "UPDATE operator_commands SET instruction='corrupted' WHERE id=?",
                (submitted.command.id,),
            )
        with pytest.raises(OperatorCommandConflict, match="authoritative command"):
            await store.load_authoritative_active_hitl_decision(run.id, request_id)

        await store.apply_hitl_decision(
            run.id,
            expected_request_id=request_id,
            to_status=RunStatus.RUNNING,
        )
        with pytest.raises(OperatorCommandConflict, match="current pause"):
            await store.load_authoritative_active_hitl_decision(run.id, request_id)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_cancel_resolves_only_active_request_and_clears_owner(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "state.db")
    run = _run(tmp_path)
    await store.save_run(run)
    active_id, _ = await store.enter_hitl(run.id, "validator_paused", {})
    historical_id = await store.save_hitl_event(
        run.id, "validator_paused", {"not_active": True}
    )
    try:
        _, created, result = await store.submit_cancel_command(
            OperatorCommand(
                run_id=run.id,
                kind=OperatorCommandKind.CANCEL,
                actor="alice",
                reason="stop",
                idempotency_key="cancel-active-only",
            )
        )
        assert created is True
        assert result["request_id"] == active_id
        requests = {
            request["id"]: request
            for request in await store.list_hitl_events(run.id)
        }
        assert requests[active_id]["decision"] == "abort"
        assert requests[historical_id]["resolved_at"] is None
        cancelled = await store.load_run(run.id)
        assert cancelled.status == RunStatus.ABORTED
        assert cancelled.active_hitl_request_id is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_cancel_does_not_resolve_unowned_request_history(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "state.db")
    run = _run(tmp_path)
    await store.save_run(run)
    historical_id = await store.save_hitl_event(
        run.id, "validator_paused", {"historical": True}
    )
    try:
        _, _, result = await store.submit_cancel_command(
            OperatorCommand(
                run_id=run.id,
                kind=OperatorCommandKind.CANCEL,
                actor="alice",
                reason="stop",
                idempotency_key="cancel-with-history",
            )
        )
        assert result["request_id"] is None
        [historical] = await store.list_hitl_events(run.id)
        assert historical["id"] == historical_id
        assert historical["resolved_at"] is None
        assert (await store.load_run(run.id)).active_hitl_request_id is None
    finally:
        await store.close()


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
async def test_idempotency_keys_are_run_scoped_and_payload_bound(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "state.db")
    first_run = _run(tmp_path).model_copy(update={"id": "run-one"})
    second_run = _run(tmp_path).model_copy(update={"id": "run-two"})
    await store.save_run(first_run)
    await store.save_run(second_run)
    try:
        first, first_created = await store.create_operator_command(
            OperatorCommand(
                run_id=first_run.id,
                kind=OperatorCommandKind.CANCEL,
                actor="alice",
                idempotency_key="shared-key",
            )
        )
        second, second_created = await store.create_operator_command(
            OperatorCommand(
                run_id=second_run.id,
                kind=OperatorCommandKind.CANCEL,
                actor="bob",
                idempotency_key="shared-key",
            )
        )
        assert first_created and second_created and first.id != second.id
        with pytest.raises(OperatorCommandConflict, match="idempotency key"):
            await store.create_operator_command(
                OperatorCommand(
                    run_id=first_run.id,
                    kind=OperatorCommandKind.STEER,
                    actor="alice",
                    instruction="different operation",
                    idempotency_key="shared-key",
                )
            )

        first_request = await store.save_hitl_event(first_run.id, "validator_paused", {})
        second_request = await store.save_hitl_event(second_run.id, "validator_paused", {})
        _, first_resolved = await store.resolve_hitl_event(
            first_request,
            action="abort",
            actor="alice",
            reason="unsafe",
            instruction="",
            idempotency_key="shared-decision",
        )
        _, second_resolved = await store.resolve_hitl_event(
            second_request,
            action="approve",
            actor="bob",
            reason="safe",
            instruction="",
            idempotency_key="shared-decision",
        )
        assert first_resolved and second_resolved
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


class _BlockingUnsupportedAdapter(MockAgent):
    def __init__(self) -> None:
        super().__init__()
        self.started = __import__("asyncio").Event()

    async def run_session(self, *, cancel_token, **kwargs):  # type: ignore[no-untyped-def]
        import asyncio

        self.started.set()
        while not cancel_token.cancelled:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        return SessionRunResult(status=SessionStatus.TIMEOUT, error=cancel_token.reason)


@pytest.mark.asyncio
async def test_attempt_consumes_steer_then_cancel_commands(tmp_path: Path) -> None:
    import asyncio

    store = SqliteStore(tmp_path / "state.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    run = _run(tmp_path / "workspace")
    run.workspace_path.mkdir(exist_ok=True)
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
async def test_unsupported_production_adapter_does_not_ack_steer(
    tmp_path: Path,
) -> None:
    import asyncio

    store = SqliteStore(tmp_path / "state.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    run = _run(tmp_path / "workspace")
    run.workspace_path.mkdir()
    await store.save_run(run)
    steer, _ = await store.create_operator_command(
        OperatorCommand(
            run_id=run.id,
            kind=OperatorCommandKind.STEER,
            actor="alice",
            instruction="change direction",
            idempotency_key="unsupported-steer",
        )
    )
    agent = _BlockingUnsupportedAdapter()
    assert await agent.inject_diagnostic("probe") is False
    try:
        with patch("horizonx.core.attempt_executor.build_agent", return_value=agent):
            execution = asyncio.create_task(
                AttemptExecutor(runtime).execute(run, prompt="wait")
            )
            await agent.started.wait()
            await asyncio.sleep(0.15)
            cancel, _ = await store.create_operator_command(
                OperatorCommand(
                    run_id=run.id,
                    kind=OperatorCommandKind.CANCEL,
                    actor="alice",
                    reason="finish test",
                    idempotency_key="unsupported-steer-cancel",
                )
            )
            await asyncio.wait_for(execution, timeout=1)
        commands = {item.id: item for item in await store.list_operator_commands(run.id)}
        assert commands[steer.id].consumed_at is None
        assert commands[cancel.id].consumed_at is not None
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
        await store.submit_active_hitl_decision(
            OperatorCommand(
                run_id=run.id,
                kind=OperatorCommandKind.DECISION,
                actor="alice",
                reason="reviewed",
                instruction="continue carefully",
                payload={
                    "request_id": requests[0]["id"],
                    "action": "approve",
                },
                idempotency_key="web-approve-1",
            )
        )
        decision = await __import__("asyncio").wait_for(decision_task, timeout=1)
        assert decision.operator == "alice"
        assert decision.instruction == "continue carefully"
        resumed = await store.load_run(run.id)
        assert resumed.status == RunStatus.RUNNING
        assert resumed.active_hitl_request_id is None
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
    request_id, _ = await app.state.store.enter_hitl(
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
            conflicting_payload = {
                **payload,
                "trigger_id": "different-callback",
                "actions": [
                    {
                        "action_id": "hitl_abort",
                        "value": f"{request_id}:abort",
                    }
                ],
            }
            conflicting_body = urlencode(
                {"payload": json.dumps(conflicting_payload)}
            ).encode()
            conflicting_signature = "v0=" + hmac.new(
                secret.encode(),
                f"v0:{timestamp}:".encode() + conflicting_body,
                hashlib.sha256,
            ).hexdigest()
            conflict = await client.post(
                "/api/hitl/slack/interactions",
                content=conflicting_body,
                headers={**headers, "x-slack-signature": conflicting_signature},
            )
            await app.state.store.apply_hitl_decision(
                run.id,
                expected_request_id=request_id,
                to_status=RunStatus.RUNNING,
            )
            next_request_id, _ = await app.state.store.enter_hitl(
                run.id, "validator_paused", {"generation": 2}
            )
            stale = await client.post(
                "/api/hitl/slack/interactions", content=body, headers=headers
            )
        assert first.json()["status"] == "resolved"
        assert duplicate.json()["status"] == "duplicate"
        assert conflict.status_code == 409
        assert stale.status_code == 409
        assert (await app.state.store.load_run(run.id)).active_hitl_request_id == (
            next_request_id
        )
        resolved = await app.state.store.find_hitl_event(request_id)
        assert resolved["operator"] == "U123"
        commands = await app.state.store.list_operator_commands(run.id)
        assert len(commands) == 1
        events = await app.state.store.list_events(
            run.id, event_type="hitl.resolved"
        )
        assert len(events) == 1 and isinstance(events[0].sequence, int)
    finally:
        await app.state.store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict_kind", ["approve_abort", "modify_instruction"])
async def test_concurrent_slack_decisions_have_one_authoritative_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    conflict_kind: str,
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
    request_id, _ = await app.state.store.enter_hitl(
        run.id, "validator_paused", {"race": conflict_kind}
    )
    if conflict_kind == "approve_abort":
        payloads = [
            {"type": "block_actions", "trigger_id": "race-approve",
             "user": {"id": "U-approve"}, "actions": [{
                 "action_id": "hitl_approve", "value": f"{request_id}:approve"
             }]},
            {"type": "block_actions", "trigger_id": "race-abort",
             "user": {"id": "U-abort"}, "actions": [{
                 "action_id": "hitl_abort", "value": f"{request_id}:abort"
             }]},
        ]
    else:
        payloads = [
            {"type": "view_submission", "user": {"id": "U-modify"},
             "view": {"id": "race-modify-a",
                      "callback_id": "hitl_modify_submission",
                      "private_metadata": request_id, "state": {"values": {
                          "modify_instruction": {"instruction": {
                              "value": "use path A"
                          }}
                      }}}},
            {"type": "view_submission", "user": {"id": "U-modify"},
             "view": {"id": "race-modify-b",
                      "callback_id": "hitl_modify_submission",
                      "private_metadata": request_id, "state": {"values": {
                          "modify_instruction": {"instruction": {
                              "value": "use path B"
                          }}
                      }}}},
        ]
    secret = "race-secret"
    monkeypatch.setenv("HORIZONX_SLACK_SIGNING_SECRET", secret)

    def signed(payload: dict[str, object]) -> tuple[bytes, dict[str, str]]:
        body = urlencode({"payload": json.dumps(payload)}).encode()
        timestamp = str(int(time.time()))
        signature = "v0=" + hmac.new(
            secret.encode(), f"v0:{timestamp}:".encode() + body, hashlib.sha256
        ).hexdigest()
        return body, {
            "content-type": "application/x-www-form-urlencoded",
            "x-slack-request-timestamp": timestamp,
            "x-slack-signature": signature,
        }

    requests = [signed(payload) for payload in payloads]
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            responses = await asyncio.gather(
                *[
                    client.post(
                        "/api/hitl/slack/interactions", content=body, headers=headers
                    )
                    for body, headers in requests
                ]
            )
            assert sorted(response.status_code for response in responses) == [200, 409]

            [command] = await app.state.store.list_operator_commands(run.id)
            [resolved] = await app.state.store.list_hitl_events(run.id)
            [event] = await app.state.store.list_events(
                run.id, event_type="hitl.resolved"
            )
            assert command.payload["action"] == resolved["decision"]
            assert command.actor == resolved["operator"]
            assert command.instruction == resolved["instruction"]
            assert event.id == f"hitl-resolved:{request_id}"

            compatibility = json.loads(
                (Path(run.workspace_path) / ".hitl_decision.json").read_text()
            )
            assert compatibility["action"] == resolved["decision"]
            assert compatibility["operator"] == resolved["operator"]
            assert compatibility["instruction"] == resolved["instruction"]

            winner_index = next(
                index
                for index, response in enumerate(responses)
                if response.status_code == 200
            )
            winner_body, winner_headers = requests[winner_index]
            replay = await client.post(
                "/api/hitl/slack/interactions",
                content=winner_body,
                headers=winner_headers,
            )
            assert replay.status_code == 200
            assert replay.json()["status"] == "duplicate"
        assert len(await app.state.store.list_operator_commands(run.id)) == 1
    finally:
        await app.state.store.close()


@pytest.mark.asyncio
async def test_live_waiter_and_dashboard_share_one_resolved_event(
    tmp_path: Path,
) -> None:
    import asyncio

    pytest.importorskip("httpx")
    from httpx import ASGITransport, AsyncClient

    from horizonx.core.event_bus import InMemoryBus
    from horizonx.dashboard.app import create_app
    from horizonx.dashboard.routes_events import _event_gen

    app = create_app(tmp_path / "state.db", tmp_path / "workspaces")
    app.state.store = SqliteStore(tmp_path / "state.db")
    app.state.bus = InMemoryBus()
    app.state.runtime = Runtime(
        app.state.store, app.state.bus, tmp_path / "workspaces"
    )
    run = _run(tmp_path / "workspace")
    run.workspace_path.mkdir()
    await app.state.store.save_run(run)
    waiter = asyncio.create_task(
        app.state.runtime.request_hitl(
            run, reason="validator_paused", context={}
        )
    )
    try:
        for _ in range(50):
            requests = await app.state.store.list_hitl_events(run.id)
            if requests:
                break
            await asyncio.sleep(0.01)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/api/runs/{run.id}/hitl",
                json={
                    "request_id": requests[0]["id"],
                    "action": "approve",
                    "operator": "alice",
                    "idempotency_key": "live-and-route",
                },
            )
        assert response.status_code == 200
        await asyncio.wait_for(waiter, timeout=1)
        events = await app.state.store.list_events(
            run.id, event_type="hitl.resolved"
        )
        assert len(events) == 1
        replay = _event_gen(
            app.state.bus,
            store=app.state.store,
            run_id=run.id,
            after_sequence=(events[0].sequence or 0) - 1,
        )
        assert (await anext(replay))["id"] == str(events[0].sequence)
        await replay.aclose()
    finally:
        if not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        await app.state.store.close()


@pytest.mark.asyncio
async def test_cancel_acceptance_atomically_wins_and_fences_lease(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "state.db")
    run = _run(tmp_path)
    await store.save_run(run)
    request_id, _ = await store.enter_hitl(run.id, "validator_paused", {})
    leases = LeaseManager(store)
    lease = await leases.acquire(f"run:{run.id}", owner="worker", ttl_seconds=30)
    assert lease is not None
    candidate = OperatorCommand(
        run_id=run.id, kind=OperatorCommandKind.CANCEL, actor="alice",
        reason="stop now", idempotency_key="atomic-cancel",
    )
    try:
        command, created, result = await store.submit_cancel_command(candidate)
        assert created and command.consumed_at is not None
        assert result["request_id"] == request_id
        assert (await store.load_run(run.id)).status == RunStatus.ABORTED
        assert await store.get_lease(f"run:{run.id}") is None
        events = await store.list_events(run.id, event_type="hitl.resolved")
        assert [event.id for event in events] == [f"hitl-resolved:{request_id}"]

        duplicate, duplicate_created, _ = await store.submit_cancel_command(
            candidate.model_copy(update={"id": "another-id"})
        )
        assert not duplicate_created and duplicate.id == command.id
        with pytest.raises(Exception, match="already terminal"):
            await store.submit_cancel_command(
                candidate.model_copy(update={"idempotency_key": "new-cancel"})
            )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_connected_sse_observes_store_only_append(tmp_path: Path) -> None:
    import asyncio

    from horizonx.core.event_bus import Event, InMemoryBus
    from horizonx.dashboard.routes_events import _event_gen

    store = SqliteStore(tmp_path / "state.db")
    run = _run(tmp_path)
    await store.save_run(run)
    stream = _event_gen(InMemoryBus(), store=store, run_id=run.id)
    pending = asyncio.create_task(anext(stream))
    try:
        await asyncio.sleep(0.02)
        persisted = await store.append_event(Event(type="hitl.resolved", run_id=run.id))
        received = await asyncio.wait_for(pending, timeout=1)
        assert received["id"] == str(persisted.sequence)
    finally:
        await stream.aclose()
        await store.close()


@pytest.mark.asyncio
async def test_sse_drains_store_burst_before_later_live_event(tmp_path: Path) -> None:
    import asyncio

    from horizonx.core.event_bus import Event, InMemoryBus
    from horizonx.dashboard.routes_events import _event_gen

    store = SqliteStore(tmp_path / "state.db")
    bus = InMemoryBus()
    run = _run(tmp_path)
    await store.save_run(run)
    stream = _event_gen(bus, store=store, run_id=run.id)
    first_pending = asyncio.create_task(anext(stream))
    try:
        await asyncio.sleep(0.02)
        persisted = [
            await store.append_event(Event(type="step.recorded", run_id=run.id))
            for _ in range(1005)
        ]
        live = await store.append_event(Event(type="run.completed", run_id=run.id))
        await bus.publish(live)
        received = [await asyncio.wait_for(first_pending, timeout=2)]
        for _ in range(1005):
            received.append(await asyncio.wait_for(anext(stream), timeout=2))
        assert [int(item["id"]) for item in received] == [
            event.sequence for event in [*persisted, live]
        ]
        await asyncio.wait_for(stream.aclose(), timeout=1)
    finally:
        await stream.aclose()
        await store.close()


@pytest.mark.asyncio
async def test_hitl_request_and_requested_event_survive_before_publish_crash(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "state.db")
    run = _run(tmp_path)
    await store.save_run(run)
    request_id, requested = await store.save_hitl_event_and_event(
        run.id, "validator_paused", {"risk": "high"}, actor="system"
    )
    assert requested.id == f"hitl.requested:{request_id}"
    assert isinstance(requested.sequence, int)
    await store.close()

    restarted = SqliteStore(tmp_path / "state.db")
    try:
        assert (await restarted.find_hitl_event(request_id))["trigger"] == "validator_paused"
        events = await restarted.list_events(run.id, event_type="hitl.requested")
        assert [event.id for event in events] == [requested.id]
        assert events[0].sequence == requested.sequence
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_cancel_winning_before_hitl_entry_aborts_without_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import threading

    store = SqliteStore(tmp_path / "state.db")
    competing = SqliteStore(tmp_path / "state.db")
    run = _run(tmp_path / "workspace")
    run.workspace_path.mkdir()
    await store.save_run(run)
    entered = threading.Event()
    release = threading.Event()
    original_enter = store._sync_enter_hitl

    def blocked_enter(*args):  # type: ignore[no-untyped-def]
        entered.set()
        assert release.wait(timeout=1)
        return original_enter(*args)

    monkeypatch.setattr(store, "_sync_enter_hitl", blocked_enter)
    runtime = Runtime(store, workspace_root=tmp_path / "workspaces")
    request_task = asyncio.create_task(
        runtime.request_hitl(run, reason="validator_paused", context={})
    )
    assert await asyncio.to_thread(entered.wait, 1)
    await competing.submit_cancel_command(
        OperatorCommand(
            run_id=run.id, kind=OperatorCommandKind.CANCEL, actor="alice",
            reason="cancel won", idempotency_key="cancel-before-hitl",
        )
    )
    release.set()
    try:
        decision = await asyncio.wait_for(request_task, timeout=0.5)
        assert decision.action == "abort"
        assert (await store.load_run(run.id)).status == RunStatus.ABORTED
        assert await store.list_hitl_events(run.id) == []
        assert await store.list_events(run.id, event_type="hitl.requested") == []
    finally:
        await store.close()
        await competing.close()


@pytest.mark.asyncio
async def test_hitl_entry_winning_before_cancel_is_resolved_atomically_on_restart(
    tmp_path: Path,
) -> None:
    import asyncio

    from horizonx.core.event_bus import Event, InMemoryBus

    class BlockingRequestedBus(InMemoryBus):
        def __init__(self) -> None:
            super().__init__()
            self.request_committed = asyncio.Event()
            self.release = asyncio.Event()

        async def publish(self, event: Event) -> None:
            if event.type == "hitl.requested":
                self.request_committed.set()
                await self.release.wait()
            await super().publish(event)

    path = tmp_path / "state.db"
    hitl_store = SqliteStore(path)
    cancel_store = SqliteStore(path)
    run = _run(tmp_path)
    run.workspace_path.mkdir(exist_ok=True)
    await hitl_store.save_run(run)
    downstream = BlockingRequestedBus()
    runtime = Runtime(hitl_store, downstream, tmp_path / "workspaces")
    request_task = asyncio.create_task(
        runtime.request_hitl(
            run, reason="validator_paused", context={"risk": "high"}
        )
    )
    assert await asyncio.wait_for(downstream.request_committed.wait(), timeout=0.5)
    assert (await hitl_store.load_run(run.id)).status == RunStatus.PAUSED_HITL
    [request] = await hitl_store.list_hitl_events(run.id)
    request_id = request["id"]
    [requested] = await hitl_store.list_events(run.id, event_type="hitl.requested")
    await cancel_store.submit_cancel_command(
        OperatorCommand(
            run_id=run.id, kind=OperatorCommandKind.CANCEL, actor="alice",
            reason="cancel after pause", idempotency_key="cancel-after-hitl",
        )
    )
    downstream.release.set()
    decision = await asyncio.wait_for(request_task, timeout=0.5)
    assert decision.action == "abort"
    await hitl_store.close()
    await cancel_store.close()

    restarted = SqliteStore(path)
    try:
        assert (await restarted.load_run(run.id)).status == RunStatus.ABORTED
        [request] = await restarted.list_hitl_events(run.id)
        assert request["id"] == request_id
        assert request["decision"] == "abort"
        assert request["resolved_at"] is not None
        requested_events = await restarted.list_events(
            run.id, event_type="hitl.requested"
        )
        resolved_events = await restarted.list_events(
            run.id, event_type="hitl.resolved"
        )
        assert [event.id for event in requested_events] == [requested.id]
        assert [event.id for event in resolved_events] == [
            f"hitl-resolved:{request_id}"
        ]
    finally:
        await restarted.close()


def test_runtime_directly_signals_and_cleans_active_cancel_tokens(tmp_path: Path) -> None:
    from horizonx.agents.base import CancelToken

    runtime = Runtime(object(), workspace_root=tmp_path)
    token = CancelToken()
    runtime.register_cancel_token("run-live", token)
    runtime.notify_operator_command("run-live", "operator_cancel:urgent")
    assert token.cancelled and token.reason == "operator_cancel:urgent"
    runtime.unregister_cancel_token("run-live", token)
    assert "run-live" not in runtime._active_cancel_tokens


@pytest.mark.asyncio
async def test_http_cancel_directly_reaches_runtime_owned_attempt(tmp_path: Path) -> None:
    import asyncio

    pytest.importorskip("httpx")
    from httpx import ASGITransport, AsyncClient

    from horizonx.core.event_bus import InMemoryBus
    from horizonx.dashboard.app import create_app

    app = create_app(tmp_path / "state.db", tmp_path / "workspaces")
    app.state.store = SqliteStore(tmp_path / "state.db")
    app.state.bus = InMemoryBus()
    app.state.runtime = Runtime(app.state.store, app.state.bus, tmp_path / "workspaces")
    run = _run(tmp_path / "workspace")
    run.workspace_path.mkdir()
    await app.state.store.save_run(run)
    agent = _CommandAwareAgent()
    try:
        with patch("horizonx.core.attempt_executor.build_agent", return_value=agent):
            execution = asyncio.create_task(
                AttemptExecutor(app.state.runtime).execute(run, prompt="wait")
            )
            await agent.started.wait()
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    f"/api/runs/{run.id}/cancel",
                    headers={"idempotency-key": "live-http-cancel"},
                )
            result = await asyncio.wait_for(execution, timeout=0.5)
        assert response.json()["status"] == "accepted"
        assert result.attempt.status == AttemptStatus.ABORTED
        assert result.agent.error.startswith("operator_cancel:")
        run.status = RunStatus.COMPLETED
        await app.state.store.save_run(run)
        assert (await app.state.store.load_run(run.id)).status == RunStatus.ABORTED
    finally:
        await app.state.store.close()


@pytest.mark.asyncio
async def test_slack_modify_opens_modal_then_submission_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("httpx")
    from httpx import ASGITransport, AsyncClient

    from horizonx.core.event_bus import InMemoryBus
    from horizonx.dashboard import routes_hitl
    from horizonx.dashboard.app import create_app

    app = create_app(tmp_path / "state.db", tmp_path / "workspaces")
    app.state.store = SqliteStore(tmp_path / "state.db")
    app.state.bus = InMemoryBus()
    app.state.runtime = Runtime(app.state.store, app.state.bus, tmp_path / "workspaces")
    run = _run(tmp_path)
    await app.state.store.save_run(run)
    request_id, _ = await app.state.store.enter_hitl(
        run.id, "validator_paused", {}
    )
    opened: list[tuple[str, str]] = []

    async def fake_open(trigger_id: str, modal_request_id: str) -> None:
        opened.append((trigger_id, modal_request_id))

    monkeypatch.setattr(routes_hitl, "_open_slack_modify_modal", fake_open)
    secret = "modify-secret"
    monkeypatch.setenv("HORIZONX_SLACK_SIGNING_SECRET", secret)

    def signed(payload: dict[str, object]) -> tuple[bytes, dict[str, str]]:
        body = urlencode({"payload": json.dumps(payload)}).encode()
        timestamp = str(int(time.time()))
        signature = "v0=" + hmac.new(
            secret.encode(), f"v0:{timestamp}:".encode() + body, hashlib.sha256
        ).hexdigest()
        return body, {"x-slack-request-timestamp": timestamp,
                      "x-slack-signature": signature,
                      "content-type": "application/x-www-form-urlencoded"}

    button = {"type": "block_actions", "trigger_id": "trigger-1",
              "user": {"id": "U1"}, "actions": [{"action_id": "hitl_modify",
              "value": f"{request_id}:modify"}]}
    submission = {"type": "view_submission", "user": {"id": "U1"},
                  "view": {"id": "view-1", "callback_id": "hitl_modify_submission",
                  "private_metadata": request_id, "state": {"values": {
                  "modify_instruction": {"instruction": {"value": "Use the safe path"}}
                  }}}}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            body, headers = signed(button)
            first = await client.post("/api/hitl/slack/interactions", content=body, headers=headers)
            body, headers = signed(submission)
            second = await client.post("/api/hitl/slack/interactions", content=body, headers=headers)
            duplicate = await client.post("/api/hitl/slack/interactions", content=body, headers=headers)
        assert first.json()["status"] == "modal_opened"
        assert opened == [("trigger-1", request_id)]
        assert second.json()["status"] == "resolved"
        assert duplicate.json()["status"] == "duplicate"
        resolved = await app.state.store.find_hitl_event(request_id)
        assert resolved["decision"] == "modify"
        assert resolved["instruction"] == "Use the safe path"
    finally:
        await app.state.store.close()


@pytest.mark.asyncio
async def test_slack_modal_token_falls_back_to_existing_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    from horizonx.dashboard.routes_hitl import (
        _open_slack_modify_modal,
        _slack_modal_token,
    )

    captured_headers: dict[str, str] = {}

    class FakeResponse:
        is_success = True

        @staticmethod
        def json() -> dict[str, bool]:
            return {"ok": True}

    class FakeClient:
        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, *args):  # type: ignore[no-untyped-def]
            return None

        async def post(self, _url, *, headers, json):  # type: ignore[no-untyped-def]
            captured_headers.update(headers)
            assert json["trigger_id"] == "trigger-existing-token"
            return FakeResponse()

    monkeypatch.delenv("HORIZONX_SLACK_BOT_TOKEN", raising=False)
    monkeypatch.setenv("HORIZONX_SLACK_TOKEN", "xoxb-existing")
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    assert _slack_modal_token() == "xoxb-existing"
    await _open_slack_modify_modal("trigger-existing-token", "request-1")
    assert captured_headers == {"authorization": "Bearer xoxb-existing"}

    monkeypatch.setenv("HORIZONX_SLACK_BOT_TOKEN", "xoxb-override")
    assert _slack_modal_token() == "xoxb-override"


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
            conflict = await client.post(
                f"/api/runs/{run.id}/commands",
                headers={
                    "authorization": "Bearer operator-secret",
                    "idempotency-key": "steer-route-1",
                    "x-horizonx-actor": "alice",
                },
                json={"kind": "steer", "instruction": "different instruction"},
            )
            await app.state.store.transition_run(run.id, RunStatus.ABORTED)
            terminal_duplicate = await client.post(
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
        assert conflict.status_code == 409
        assert terminal_duplicate.status_code == 202
        assert terminal_duplicate.json()["status"] == "duplicate"
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


@pytest.mark.asyncio
async def test_live_cancel_stops_unbounded_hitl_wait_and_releases_lease(
    tmp_path: Path,
) -> None:
    import asyncio

    store = SqliteStore(tmp_path / "state.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    run = _run(tmp_path / "workspace")
    run.workspace_path.mkdir()
    await store.save_run(run)
    session = Session(id="session-hitl-cancel", run_id=run.id, sequence_index=0)
    await store.save_session(session)
    attempt = await store.create_attempt(
        AttemptRecord(
            run_id=run.id,
            session_id=session.id,
            status=AttemptStatus.PAUSED_HITL,
            provider="mock",
            model="mock",
            workspace_path=run.workspace_path,
        )
    )
    leases = LeaseManager(store)
    lease = await leases.acquire(f"run:{run.id}", owner="live-worker", ttl_seconds=2)
    assert lease is not None

    async def wait_with_lease():  # type: ignore[no-untyped-def]
        async with leases.maintain(lease, ttl_seconds=2):
            return await runtime.request_hitl(
                run, reason="validator_paused", context={}
            )

    task = asyncio.create_task(wait_with_lease())
    try:
        for _ in range(50):
            if await store.list_hitl_events(run.id):
                break
            await asyncio.sleep(0.01)
        command, _ = await store.create_operator_command(
            OperatorCommand(
                run_id=run.id,
                kind=OperatorCommandKind.CANCEL,
                actor="alice",
                reason="stop while paused",
                idempotency_key="live-hitl-cancel",
            )
        )
        decision = await asyncio.wait_for(task, timeout=0.5)
        assert decision.action == "abort"
        assert (await store.load_run(run.id)).status == RunStatus.ABORTED
        assert (await store.load_attempt(attempt.id)).status == AttemptStatus.ABORTED
        saved = (await store.list_operator_commands(run.id))[0]
        assert saved.id == command.id and saved.consumed_at is not None
        hitl = (await store.list_hitl_events(run.id))[0]
        assert hitl["decision"] == "abort"
        assert hitl["operator"] == "alice"
        assert hitl["reason"] == "stop while paused"
        assert hitl["instruction"] == ""
        assert hitl["resolved_at"] is not None
        assert await store.get_lease(f"run:{run.id}") is None
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await store.close()
