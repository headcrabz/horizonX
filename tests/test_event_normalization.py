"""Provider events normalize to one stable semantic representation."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

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
