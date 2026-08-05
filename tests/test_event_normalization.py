"""Provider events normalize to one stable semantic representation."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from horizonx.agents.claude_code import ClaudeCodeAgent, ClaudeCodeConfig
from horizonx.agents.codex import CodexAgent, CodexConfig
from horizonx.core.recorder import TrajectoryRecorder
from horizonx.core.types import Session, Step, StepType
from horizonx.events.normalizers import normalize_step


def _step(*, provider: str, tool_name: str, content: dict) -> Step:
    return Step(
        session_id="session-1",
        sequence=1,
        type=StepType.TOOL_CALL,
        tool_name=tool_name,
        content={"provider": provider, **content},
    )


def test_claude_and_codex_commands_normalize_to_equivalent_events() -> None:
    claude = _step(
        provider="claude-code",
        tool_name="Bash",
        content={"input": {"command": "pytest -q"}},
    )
    codex = _step(
        provider="codex",
        tool_name="command_execution",
        content={"command": "pytest -q"},
    )

    assert normalize_step(claude).model_dump(exclude={"provider_kind"}) == normalize_step(
        codex
    ).model_dump(exclude={"provider_kind"})


def test_normalizer_derives_edit_target_and_changed_content_digest() -> None:
    event = normalize_step(
        _step(
            provider="claude-code",
            tool_name="Edit",
            content={"input": {"file_path": "src/app.py", "old_string": "A", "new_string": "B"}},
        )
    )

    assert event.category == "edit"
    assert event.target == "src/app.py"
    assert event.changed_file_digest


@pytest.mark.asyncio
async def test_recorder_persists_canonical_event_alongside_raw_provider_payload() -> None:
    store = MagicMock()
    store.save_step = AsyncMock()
    store.load_run = AsyncMock(
        return_value=SimpleNamespace(workspace_path=Path("/nonexistent"))
    )
    bus = MagicMock()
    bus.publish = AsyncMock()
    recorder = TrajectoryRecorder(store, bus)
    recorder._append_jsonl = AsyncMock()  # type: ignore[method-assign]
    session = Session(id="session-1", run_id="run-1", sequence_index=0)
    step = _step(
        provider="codex",
        tool_name="command_execution",
        content={"command": "pytest -q"},
    )

    await recorder.record(session, step)

    assert step.content["command"] == "pytest -q"
    assert step.canonical is not None
    assert step.canonical["tool_name"] == "shell"


@pytest.mark.asyncio
async def test_recorded_provider_fixtures_flow_through_adapter_and_recorder() -> None:
    fixtures = Path(__file__).parent / "fixtures" / "provider_events"
    claude_event = json.loads((fixtures / "claude_edit.json").read_text())[0]
    codex_event = json.loads((fixtures / "codex_edit.json").read_text())[0]
    claude_step = ClaudeCodeAgent(ClaudeCodeConfig())._event_to_steps(claude_event, 0, "s1")[0]
    codex_step = CodexAgent(CodexConfig())._event_to_steps(codex_event, 0, "s1")[0]
    store = MagicMock()
    store.save_step = AsyncMock()
    store.load_run = AsyncMock(
        return_value=SimpleNamespace(workspace_path=Path("/nonexistent"))
    )
    bus = MagicMock()
    bus.publish = AsyncMock()
    recorder = TrajectoryRecorder(store, bus)
    recorder._append_jsonl = AsyncMock()  # type: ignore[method-assign]
    session = Session(id="s1", run_id="r1", sequence_index=0)
    await recorder.record(session, claude_step)
    await recorder.record(session, codex_step)
    assert claude_step.canonical and claude_step.canonical["category"] == "edit"
    assert codex_step.type == StepType.FILE_CHANGE
    assert codex_step.canonical and codex_step.canonical["category"] == "edit"
