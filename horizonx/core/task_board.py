"""task_board.jsonl — append-only coordination log for parallel agents.

Written to the run workspace after goal claim/complete/fail events.
The last-N entries are injected into session prompts as TEAM_STATUS so
parallel agents see what their peers are working on.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from horizonx.core.types import utcnow

TaskBoardEvent = Literal["claimed", "completed", "failed", "skipped"]


def append_task_board(
    workspace: Path,
    *,
    event: TaskBoardEvent,
    goal_id: str,
    session_id: str,
    agent_type: str = "unknown",
    extra: dict[str, Any] | None = None,
) -> None:
    """Append one event line to task_board.jsonl in the workspace."""
    entry: dict[str, Any] = {
        "ts": utcnow().isoformat(),
        "event": event,
        "goal_id": goal_id,
        "session_id": session_id,
        "agent": agent_type,
    }
    if extra:
        entry.update(extra)
    board_path = workspace / "task_board.jsonl"
    with board_path.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def read_task_board(workspace: Path, last_n: int = 10) -> list[dict[str, Any]]:
    """Return the most recent N events from task_board.jsonl."""
    board_path = workspace / "task_board.jsonl"
    if not board_path.exists():
        return []
    lines = board_path.read_text().strip().splitlines()
    recent = lines[-last_n:] if len(lines) > last_n else lines
    events = []
    for line in recent:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return events
