"""TrajectoryRecorder — append-only JSONL + DB + bus.

Every step persists immediately. JSONL on disk is the source of truth;
DB is the query interface. See docs/LONG_HORIZON_AGENT.md §16.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from horizonx.core.event_bus import Event, EventBus
from horizonx.core.types import Session, Step, StepType
from horizonx.events.normalizers import normalize_step


class TrajectoryRecorder:
    def __init__(self, store: Any, bus: EventBus):
        self.store = store
        self.bus = bus
        self._pending_edits: dict[tuple[str, str], str] = {}

    def discard_pending_edits(self, session_id: str) -> None:
        self._pending_edits = {
            key: target
            for key, target in self._pending_edits.items()
            if key[0] != session_id
        }

    async def record(self, session: Session, step: Step) -> None:
        preliminary = normalize_step(step)
        correlation = preliminary.correlation_id
        is_edit_request = (
            step.type == StepType.TOOL_CALL
            and preliminary.category == "edit"
            and correlation is not None
            and preliminary.target is not None
        )
        if is_edit_request:
            assert correlation is not None
            assert preliminary.target is not None
            self._pending_edits[(session.id, correlation)] = preliminary.target
        pending_target = (
            self._pending_edits.pop((session.id, correlation), None)
            if step.type == StepType.OBSERVATION and correlation is not None
            else None
        )
        changed_file_digest = await self._changed_file_digest(
            session, step, target_override=pending_target
        )
        canonical = normalize_step(
            step,
            changed_file_digest=changed_file_digest,
            include_declared_change=not is_edit_request,
        )
        if pending_target is not None:
            canonical = canonical.model_copy(
                update={
                    "category": "edit",
                    "target": pending_target,
                    "changed_file_digest": (
                        None if step.content.get("is_error") else changed_file_digest
                    ),
                }
            )
        step.canonical = canonical.model_dump(mode="json")
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

    async def _changed_file_digest(
        self, session: Session, step: Step, *, target_override: str | None = None
    ) -> str | None:
        """Snapshot a provider-reported edit from the run workspace when available."""
        event = normalize_step(step)
        if step.type != StepType.FILE_CHANGE and target_override is None:
            return None
        run = await self.store.load_run(session.run_id)
        workspace = Path(run.workspace_path).resolve()
        candidate = (workspace / (target_override or event.target or "")).resolve()
        try:
            candidate.relative_to(workspace)
        except ValueError:
            return None
        try:
            return await asyncio.to_thread(self._read_changed_file, candidate)
        except OSError:
            return None

    @staticmethod
    def _read_changed_file(candidate: Path) -> str:
        """Read a provider-completed edit without unbounded memory growth."""
        digest = hashlib.sha256()
        with candidate.open("rb") as changed_file:
            while chunk := changed_file.read(64 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    async def _append_jsonl(self, session: Session, step: Step) -> None:
        # Workspace path is on Run; we infer from session via store
        run = await self.store.load_run(session.run_id)
        path = Path(run.workspace_path) / "trajectory.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(step.model_dump(mode="json"), default=str)
        with path.open("a") as f:
            f.write(line + "\n")
