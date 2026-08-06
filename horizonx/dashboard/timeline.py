"""Typed, durable projections for the run timeline API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from horizonx.storage.sqlite import SqliteStore


class TimelineEntities(BaseModel):
    """Only relationships actually recorded with an event."""

    run_id: str
    goal_id: str | None = None
    attempt_id: str | None = None
    session_id: str | None = None
    validation_id: str | None = None
    evidence_id: str | None = None
    hitl_id: str | None = None
    spin_report_id: str | None = None
    command_id: str | None = None


class TimelineEventSummary(BaseModel):
    sequence: int
    id: str
    type: str
    timestamp: str
    entities: TimelineEntities


class TimelinePage(BaseModel):
    run_id: str
    run_status: str
    events: list[TimelineEventSummary]
    next_after: int | None = None


class TimelineEventDetail(BaseModel):
    sequence: int
    id: str
    type: str
    timestamp: str
    entities: TimelineEntities
    payload: dict[str, Any]


class TimelinePlayback(BaseModel):
    run_id: str
    sequence: int
    graph_version: int | None = None
    graph_digest: str | None = None
    graph: dict[str, Any] | None = None


class TimelineProjection:
    """Read model built from SQLite events and graph snapshots only."""

    def __init__(self, store: SqliteStore) -> None:
        self.store = store

    async def _run_status(self, run_id: str) -> str:
        try:
            run = await self.store.load_run(run_id)
        except KeyError:
            raise KeyError(f"run {run_id!r} not found") from None
        return run.status.value

    @staticmethod
    def _entities(row: dict[str, Any]) -> TimelineEntities:
        return TimelineEntities(
            run_id=row["run_id"], goal_id=row.get("goal_id"),
            attempt_id=row.get("attempt_id"), session_id=row.get("session_id"),
            validation_id=row.get("validation_id"), evidence_id=row.get("evidence_id"),
            hitl_id=row.get("hitl_id"), spin_report_id=row.get("spin_report_id"),
            command_id=row.get("command_id"),
        )

    async def page(self, run_id: str, *, after: int, limit: int) -> TimelinePage:
        status = await self._run_status(run_id)
        rows = await self.store.list_event_summaries(
            run_id, after_sequence=after, limit=limit + 1
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        events = [
            TimelineEventSummary(
                sequence=row["sequence"], id=row["id"], type=row["type"],
                timestamp=row["timestamp"], entities=self._entities(row),
            )
            for row in rows
        ]
        return TimelinePage(
            run_id=run_id, run_status=status, events=events,
            next_after=events[-1].sequence if has_more and events else None,
        )

    async def event_detail(self, run_id: str, sequence: int) -> TimelineEventDetail:
        await self._run_status(run_id)
        event = await self.store.get_event(run_id, sequence)
        if event is None:
            raise KeyError(f"event {sequence} not found for run {run_id!r}")
        return TimelineEventDetail(
            sequence=event.sequence or 0, id=event.id, type=event.type,
            timestamp=event.timestamp.isoformat(),
            entities=TimelineEntities(
                run_id=event.run_id or run_id,
                goal_id=event.goal_id or event.payload.get("goal_id"),
                attempt_id=event.attempt_id, session_id=event.session_id,
                validation_id=event.payload.get("validation_id"),
                evidence_id=event.payload.get("evidence_id"),
                hitl_id=event.payload.get("hitl_id") or event.payload.get("request_id"),
                spin_report_id=event.payload.get("spin_report_id"),
                command_id=event.payload.get("command_id"),
            ),
            payload=event.payload,
        )

    async def playback(self, run_id: str, *, sequence: int) -> TimelinePlayback:
        await self._run_status(run_id)
        snapshot = await self.store.graph_snapshot_at_sequence(run_id, sequence)
        if snapshot is None:
            return TimelinePlayback(run_id=run_id, sequence=sequence)
        return TimelinePlayback(
            run_id=run_id, sequence=sequence,
            graph_version=snapshot["version"], graph_digest=snapshot["digest"],
            graph=snapshot["graph"],
        )
