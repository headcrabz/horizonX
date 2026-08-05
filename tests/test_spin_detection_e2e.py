"""Spin analysis consumes canonical provider events, not adapter payload shapes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from horizonx.agents.claude_code import ClaudeCodeAgent, ClaudeCodeConfig
from horizonx.agents.codex import CodexAgent, CodexConfig
from horizonx.core.event_bus import InMemoryBus
from horizonx.core.recorder import TrajectoryRecorder
from horizonx.core.spin_detector import EditRevertLayer, ToolThrashingLayer
from horizonx.core.types import AgentConfig, Run, Session, Step, StepType, StrategyConfig, Task
from horizonx.storage.sqlite import SqliteStore


def _canonical_step(seq: int, *, category: str, target: str, changed: str | None = None, result: str | None = None) -> Step:
    return Step(
        session_id="s1", sequence=seq, type=StepType.TOOL_CALL, tool_name="adapter-specific",
        content={}, canonical={"kind": "tool_call", "category": category, "target": target,
                               "changed_file_digest": changed, "result_digest": result},
    )


@pytest.mark.asyncio
async def test_edit_revert_detects_actual_a_to_b_to_a_from_canonical_digests() -> None:
    store = MagicMock()
    store.recent_steps = AsyncMock(return_value=[
        _canonical_step(1, category="edit", target="app.py", changed="A"),
        _canonical_step(2, category="edit", target="app.py", changed="B"),
        _canonical_step(3, category="edit", target="app.py", changed="A"),
    ])

    report = await EditRevertLayer().check(Session(run_id="r1", sequence_index=0), store)

    assert report.detected


@pytest.mark.asyncio
async def test_repeated_polling_with_distinct_result_digests_is_not_thrashing() -> None:
    store = MagicMock()
    store.recent_steps = AsyncMock(return_value=[
        _canonical_step(i, category="read", target="/status", result=f"digest-{i}") for i in range(5)
    ])

    report = await ToolThrashingLayer(no_progress_threshold=5).check(
        Session(run_id="r1", sequence_index=0), store
    )

    assert not report.detected


@pytest.mark.asyncio
async def test_same_static_output_from_different_targets_is_not_grouped_globally() -> None:
    store = MagicMock()
    store.recent_steps = AsyncMock(
        return_value=[
            _canonical_step(i, category="read", target=f"file-{i}.txt", result="same")
            for i in range(5)
        ]
    )
    report = await ToolThrashingLayer(no_progress_threshold=5).check(
        Session(run_id="r1", sequence_index=0), store
    )
    assert not report.detected


@pytest.mark.asyncio
async def test_unchanged_status_poll_is_recognized_as_legitimate() -> None:
    store = MagicMock()
    store.recent_steps = AsyncMock(
        return_value=[
            _canonical_step(i, category="read", target="/status", result="pending")
            for i in range(5)
        ]
    )
    report = await ToolThrashingLayer(no_progress_threshold=5).check(
        Session(run_id="r1", sequence_index=0), store
    )
    assert not report.detected


async def _record_edit_fixture(
    tmp_path: Path, fixture_name: str, agent: object
) -> tuple[list[Step], bool]:
    fixture_path = Path(__file__).parent / "fixtures" / "provider_events" / fixture_name
    events = json.loads(fixture_path.read_text())
    store = SqliteStore(tmp_path / f"{fixture_name}.db")
    workspace = tmp_path / fixture_name
    workspace.mkdir()
    run = Run(
        task=Task(
            id=fixture_name,
            name=fixture_name,
            prompt="fixture",
            strategy=StrategyConfig(kind="single"),
            agent=AgentConfig(type="mock", model="mock"),
        ),
        workspace_path=workspace,
    )
    session = Session(id=f"session-{fixture_name}", run_id=run.id, sequence_index=0)
    await store.save_run(run)
    await store.save_session(session)
    recorder = TrajectoryRecorder(store, InMemoryBus())
    contents = iter(("A", "B", "A"))
    sequence = 0
    try:
        for event in events:
            parsed = agent._event_to_steps(event, sequence, session.id)  # type: ignore[attr-defined]
            assert len(parsed) == 1
            step = parsed[0]
            if step.type == StepType.FILE_CHANGE:
                (workspace / "app.py").write_text(next(contents))
            step.sequence = sequence
            await recorder.record(session, step)
            if step.type == StepType.TOOL_CALL and step.tool_name == "Edit":
                # Claude reports the requested new value before the tool result.
                (workspace / "app.py").write_text(str(step.content["input"]["new_string"]))
            sequence += 1
        steps = await store.recent_steps(session.id, 50)
        report = await EditRevertLayer().check(session, store)
        return steps, report.detected
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_provider_edit_fixtures_have_equivalent_persisted_a_to_b_to_a_digests(
    tmp_path: Path,
) -> None:
    claude_steps, claude_detected = await _record_edit_fixture(
        tmp_path, "claude_edit.json", ClaudeCodeAgent(ClaudeCodeConfig())
    )
    codex_steps, codex_detected = await _record_edit_fixture(
        tmp_path, "codex_edit.json", CodexAgent(CodexConfig())
    )

    def semantics(steps: list[Step]) -> list[tuple[object, object]]:
        return [
            (step.canonical["target"], step.canonical["changed_file_digest"])
            for step in steps
            if step.canonical
            and step.canonical["category"] == "edit"
            and step.canonical["changed_file_digest"] is not None
        ]

    assert semantics(claude_steps) == semantics(codex_steps)
    assert claude_detected is True
    assert codex_detected is True


@pytest.mark.asyncio
async def test_failed_claude_edit_does_not_record_requested_content_as_applied(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "failed-edit.db")
    workspace = tmp_path / "failed-edit"
    workspace.mkdir()
    (workspace / "app.py").write_text("A")
    run = Run(task=Task(id="e", name="e", prompt="e", strategy=StrategyConfig(kind="single"), agent=AgentConfig(type="mock", model="mock")), workspace_path=workspace)
    session = Session(id="s", run_id=run.id, sequence_index=0)
    await store.save_run(run)
    await store.save_session(session)
    recorder = TrajectoryRecorder(store, InMemoryBus())
    agent = ClaudeCodeAgent(ClaudeCodeConfig())
    events = [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "edit-fail", "name": "Edit", "input": {"file_path": "app.py", "old_string": "A", "new_string": "B"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "edit-fail", "content": "old text not found", "is_error": True}]}},
    ]
    try:
        sequence = 0
        for raw in events:
            for step in agent._event_to_steps(raw, sequence, session.id):
                step.sequence = sequence
                await recorder.record(session, step)
                sequence += 1
        steps = await store.recent_steps(session.id, 10)
        assert all(
            not step.canonical or step.canonical["changed_file_digest"] is None
            for step in steps
        )
        assert not (await EditRevertLayer().check(session, store)).detected
    finally:
        await store.close()


def test_interrupted_session_discards_pending_edit_correlations() -> None:
    recorder = TrajectoryRecorder(MagicMock(), InMemoryBus())
    recorder._pending_edits[("interrupted", "tool-1")] = "app.py"
    recorder._pending_edits[("active", "tool-2")] = "other.py"
    recorder.discard_pending_edits("interrupted")
    assert recorder._pending_edits == {("active", "tool-2"): "other.py"}


@pytest.mark.asyncio
async def test_fixture_driven_unchanged_status_poll_does_not_trigger_spin(
    tmp_path: Path,
) -> None:
    fixture = Path(__file__).parent / "fixtures" / "provider_events" / "claude_reads.json"
    events = json.loads(fixture.read_text())
    store = SqliteStore(tmp_path / "reads.db")
    workspace = tmp_path / "reads"
    workspace.mkdir()
    run = Run(
        task=Task(
            id="reads",
            name="reads",
            prompt="poll",
            strategy=StrategyConfig(kind="single"),
            agent=AgentConfig(type="mock", model="mock"),
        ),
        workspace_path=workspace,
    )
    session = Session(id="session-reads", run_id=run.id, sequence_index=0)
    await store.save_run(run)
    await store.save_session(session)
    recorder = TrajectoryRecorder(store, InMemoryBus())
    agent = ClaudeCodeAgent(ClaudeCodeConfig())
    sequence = 0
    try:
        for event in events:
            for step in agent._event_to_steps(event, sequence, session.id):
                step.sequence = sequence
                await recorder.record(session, step)
                sequence += 1
        report = await ToolThrashingLayer(no_progress_threshold=5).check(session, store)
        assert report.detected is False
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_disappearing_edit_target_does_not_break_recording(tmp_path: Path) -> None:
    store = MagicMock()
    store.load_run = AsyncMock(return_value=MagicMock(workspace_path=tmp_path))
    store.save_step = AsyncMock()
    bus = MagicMock()
    bus.publish = AsyncMock()
    recorder = TrajectoryRecorder(store, bus)
    recorder._append_jsonl = AsyncMock()  # type: ignore[method-assign]
    session = Session(id="s1", run_id="r1", sequence_index=0)
    step = Step(
        session_id="s1",
        sequence=0,
        type=StepType.FILE_CHANGE,
        tool_name="file_change",
        content={"changes": [{"path": "gone.py", "kind": "delete"}]},
    )

    await recorder.record(session, step)

    assert step.canonical and step.canonical["changed_file_digest"] is not None


@pytest.mark.asyncio
async def test_large_edit_target_is_hashed_in_bounded_chunks(tmp_path: Path) -> None:
    content = b"x" * (2 * 1024 * 1024)
    (tmp_path / "large.bin").write_bytes(content)
    store = MagicMock()
    store.load_run = AsyncMock(return_value=MagicMock(workspace_path=tmp_path))
    store.save_step = AsyncMock()
    bus = MagicMock()
    bus.publish = AsyncMock()
    recorder = TrajectoryRecorder(store, bus)
    recorder._append_jsonl = AsyncMock()  # type: ignore[method-assign]
    session = Session(id="s1", run_id="r1", sequence_index=0)
    step = Step(
        session_id="s1",
        sequence=0,
        type=StepType.FILE_CHANGE,
        tool_name="file_change",
        content={"changes": [{"path": "large.bin", "kind": "update"}]},
    )

    await recorder.record(session, step)

    assert step.canonical
    assert step.canonical["changed_file_digest"] == hashlib.sha256(content).hexdigest()
