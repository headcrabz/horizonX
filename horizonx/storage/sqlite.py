"""SQLite store. Persists Runs, Sessions, Steps, Goals, Validations, HITL events.

See docs/LONG_HORIZON_AGENT.md §17 for the schema.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from horizonx.core.types import (
    GateDecision,
    GoalNode,
    Run,
    Session,
    SpinReport,
    Step,
)


SCHEMA = """\
CREATE TABLE IF NOT EXISTS runs (
    id              TEXT PRIMARY KEY,
    parent_run_id   TEXT,
    task_snapshot   TEXT NOT NULL,
    status          TEXT NOT NULL,
    workspace_path  TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    completed_at    TEXT,
    current_session_id TEXT,
    goal_graph_root TEXT NOT NULL,
    cumulative      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status, started_at);

CREATE TABLE IF NOT EXISTS sessions (
    id                  TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL,
    sequence_index      INTEGER NOT NULL,
    target_goal_id      TEXT,
    status              TEXT NOT NULL,
    started_at          TEXT NOT NULL,
    completed_at        TEXT,
    steps_count         INTEGER NOT NULL DEFAULT 0,
    tokens_used         INTEGER NOT NULL DEFAULT 0,
    agent_session_id    TEXT,
    handoff_summary_path TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_run ON sessions(run_id, sequence_index);

CREATE TABLE IF NOT EXISTS steps (
    id           TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL,
    sequence     INTEGER NOT NULL,
    type         TEXT NOT NULL,
    tool_name    TEXT,
    content      TEXT NOT NULL,
    timestamp    TEXT NOT NULL,
    duration_ms  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_steps_session ON steps(session_id, sequence);

CREATE TABLE IF NOT EXISTS goals (
    id                       TEXT PRIMARY KEY,
    run_id                   TEXT NOT NULL,
    parent_id                TEXT,
    name                     TEXT NOT NULL,
    description              TEXT NOT NULL,
    verification_criteria    TEXT NOT NULL,
    status                   TEXT NOT NULL,
    attempts                 INTEGER NOT NULL DEFAULT 0,
    notes                    TEXT,
    last_updated_at          TEXT NOT NULL,
    last_updated_by_session  TEXT
);
CREATE INDEX IF NOT EXISTS idx_goals_run ON goals(run_id, status);

CREATE TABLE IF NOT EXISTS validations (
    id           TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    session_id   TEXT,
    validator    TEXT NOT NULL,
    decision     TEXT NOT NULL,
    reason       TEXT NOT NULL,
    score        REAL,
    details      TEXT,
    started_at   TEXT NOT NULL,
    duration_ms  INTEGER
);

CREATE TABLE IF NOT EXISTS hitl_events (
    id           TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    triggered_at TEXT NOT NULL,
    trigger      TEXT NOT NULL,
    context      TEXT NOT NULL,
    resolved_at  TEXT,
    decision     TEXT,
    operator     TEXT,
    instruction  TEXT
);

CREATE TABLE IF NOT EXISTS spin_reports (
    id           TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL,
    layer        TEXT NOT NULL,
    detected_at  TEXT NOT NULL,
    detail       TEXT NOT NULL,
    action_taken TEXT NOT NULL
);
"""


class SqliteStore:
    """Synchronous SQLite store. Async wrappers run in default executor."""

    def __init__(self, db_path: str | Path = "horizonx.db"):
        self.db_path = str(db_path)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    async def save_run(self, run: Run) -> None:
        from uuid import uuid4

        with self._conn() as c:
            c.execute(
                """\
                INSERT INTO runs (id, parent_run_id, task_snapshot, status, workspace_path,
                                 started_at, completed_at, current_session_id, goal_graph_root, cumulative)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    completed_at=excluded.completed_at,
                    current_session_id=excluded.current_session_id,
                    cumulative=excluded.cumulative
                """,
                (
                    run.id,
                    run.parent_run_id,
                    run.task.model_dump_json(),
                    run.status.value,
                    str(run.workspace_path),
                    run.started_at.isoformat(),
                    run.completed_at.isoformat() if run.completed_at else None,
                    run.current_session_id,
                    run.goal_graph_root,
                    run.cumulative.model_dump_json(),
                ),
            )

    async def load_run(self, run_id: str) -> Run:
        from horizonx.core.types import CumulativeMetrics, RunStatus, Task

        with self._conn() as c:
            row = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise KeyError(f"run not found: {run_id}")
        return Run(
            id=row["id"],
            parent_run_id=row["parent_run_id"],
            task=Task.model_validate_json(row["task_snapshot"]),
            status=RunStatus(row["status"]),
            workspace_path=Path(row["workspace_path"]),
            current_session_id=row["current_session_id"],
            goal_graph_root=row["goal_graph_root"],
            cumulative=CumulativeMetrics.model_validate_json(row["cumulative"] or "{}"),
        )

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    async def save_session(self, s: Session) -> None:
        with self._conn() as c:
            c.execute(
                """\
                INSERT INTO sessions (id, run_id, sequence_index, target_goal_id, status,
                                      started_at, completed_at, steps_count, tokens_used,
                                      agent_session_id, handoff_summary_path)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    completed_at=excluded.completed_at,
                    steps_count=excluded.steps_count,
                    tokens_used=excluded.tokens_used,
                    agent_session_id=excluded.agent_session_id,
                    handoff_summary_path=excluded.handoff_summary_path
                """,
                (
                    s.id,
                    s.run_id,
                    s.sequence_index,
                    s.target_goal_id,
                    s.status.value,
                    s.started_at.isoformat(),
                    s.completed_at.isoformat() if s.completed_at else None,
                    s.steps_count,
                    s.tokens_used,
                    s.agent_session_id,
                    str(s.handoff_summary_path) if s.handoff_summary_path else None,
                ),
            )

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    async def save_step(self, step: Step) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO steps (id, session_id, sequence, type, tool_name, content, timestamp, duration_ms)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    step.id,
                    step.session_id,
                    step.sequence,
                    step.type.value,
                    step.tool_name,
                    json.dumps(step.content, default=str),
                    step.timestamp.isoformat(),
                    step.duration_ms,
                ),
            )

    async def recent_steps(self, session_id: str, n: int) -> list[Step]:
        from horizonx.core.types import StepType

        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM steps WHERE session_id=? ORDER BY sequence DESC LIMIT ?",
                (session_id, n),
            ).fetchall()
        out = []
        for row in reversed(rows):
            out.append(
                Step(
                    id=row["id"],
                    session_id=row["session_id"],
                    sequence=row["sequence"],
                    type=StepType(row["type"]),
                    tool_name=row["tool_name"],
                    content=json.loads(row["content"]),
                    timestamp=row["timestamp"],
                    duration_ms=row["duration_ms"],
                )
            )
        return out

    async def recent_validator_scores(self, run_id: str, n: int) -> list[float]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT score FROM validations WHERE run_id=? AND score IS NOT NULL ORDER BY started_at DESC LIMIT ?",
                (run_id, n),
            ).fetchall()
        return list(reversed([r["score"] for r in rows]))

    # ------------------------------------------------------------------
    # Goals
    # ------------------------------------------------------------------

    async def save_goal(self, run_id: str, g: GoalNode) -> None:
        with self._conn() as c:
            c.execute(
                """\
                INSERT INTO goals (id, run_id, parent_id, name, description, verification_criteria,
                                   status, attempts, notes, last_updated_at, last_updated_by_session)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    attempts=excluded.attempts,
                    notes=excluded.notes,
                    last_updated_at=excluded.last_updated_at,
                    last_updated_by_session=excluded.last_updated_by_session
                """,
                (
                    g.id,
                    run_id,
                    g.parent_id,
                    g.name,
                    g.description,
                    json.dumps(g.verification_criteria),
                    g.status.value,
                    g.attempts,
                    g.notes,
                    g.last_updated_at.isoformat(),
                    g.last_updated_by_session,
                ),
            )

    async def load_goal(self, run_id: str, goal_id: str) -> GoalNode | None:
        from horizonx.core.types import GoalStatus

        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM goals WHERE run_id=? AND id=?", (run_id, goal_id)
            ).fetchone()
        if not row:
            return None
        return GoalNode(
            id=row["id"],
            parent_id=row["parent_id"],
            name=row["name"],
            description=row["description"],
            verification_criteria=json.loads(row["verification_criteria"]),
            status=GoalStatus(row["status"]),
            attempts=row["attempts"],
            notes=row["notes"] or "",
            last_updated_at=row["last_updated_at"],
            last_updated_by_session=row["last_updated_by_session"],
        )

    # ------------------------------------------------------------------
    # Validations / HITL / Spin
    # ------------------------------------------------------------------

    async def save_validation(self, run: Run, session: Session | None, decision: GateDecision) -> None:
        from uuid import uuid4

        with self._conn() as c:
            c.execute(
                """INSERT INTO validations (id, run_id, session_id, validator, decision, reason, score, details, started_at, duration_ms)
                   VALUES (?,?,?,?,?,?,?,?,datetime('now'),?)""",
                (
                    str(uuid4()),
                    run.id,
                    session.id if session else None,
                    decision.validator_name,
                    decision.decision.value,
                    decision.reason,
                    decision.score,
                    json.dumps(decision.details, default=str),
                    decision.duration_ms,
                ),
            )

    async def save_spin_report(self, session: Session, report: SpinReport) -> None:
        from uuid import uuid4

        with self._conn() as c:
            c.execute(
                """INSERT INTO spin_reports (id, session_id, layer, detected_at, detail, action_taken)
                   VALUES (?,?,?,datetime('now'),?,?)""",
                (
                    str(uuid4()),
                    session.id,
                    report.layer or "unknown",
                    json.dumps(report.detail, default=str),
                    report.action,
                ),
            )

    async def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, status, started_at, completed_at FROM runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
