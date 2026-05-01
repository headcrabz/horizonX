"""TrajectoryRecorder — append-only JSONL + DB + bus.

Every step persists immediately. JSONL on disk is the source of truth;
DB is the query interface. See docs/LONG_HORIZON_AGENT.md §16.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from horizonx.core.event_bus import Event, EventBus
from horizonx.core.types import Session, Step


class TrajectoryRecorder:
    def __init__(self, store: Any, bus: EventBus):
        self.store = store
        self.bus = bus

    async def record(self, session: Session, step: Step) -> None:
        await self._append_jsonl(session, step)
        await self.store.save_step(step)
        await self.bus.publish(
            Event(
                type="step.recorded",
                run_id=session.run_id,
                session_id=session.id,
                payload={
                    "type": step.type.value,
                    "tool_name": step.tool_name,
                    "sequence": step.sequence,
                },
            )
        )

    async def _append_jsonl(self, session: Session, step: Step) -> None:
        # Workspace path is on Run; we infer from session via store
        run = await self.store.load_run(session.run_id)
        path = Path(run.workspace_path) / "trajectory.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(step.model_dump(mode="json"), default=str)
        with path.open("a") as f:
            f.write(line + "\n")
