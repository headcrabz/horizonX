"""SQLite store. Persists Runs, Sessions, Steps, Goals, Validations, HITL events.

See docs/LONG_HORIZON_AGENT.md §17 for the schema.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

from horizonx.core.types import (
    GateDecision,
    GoalNode,
    Run,
    Session,
    SpinReport,
    Step,
    ValidatorConfig,
)

_T = TypeVar("_T")

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
    housekeeping_steps  INTEGER NOT NULL DEFAULT 0,
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
    last_updated_by_session  TEXT,
    assigned_to_session      TEXT,
    validators               TEXT,
    inherit_validators       INTEGER NOT NULL DEFAULT 1
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

CREATE TABLE IF NOT EXISTS pending_runs (
    run_id      TEXT PRIMARY KEY,
    task_json   TEXT NOT NULL,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status      TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS workspace_usage (
    id           TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    run_id       TEXT NOT NULL,
    date         TEXT NOT NULL,
    tokens_in    INTEGER NOT NULL DEFAULT 0,
    tokens_out   INTEGER NOT NULL DEFAULT 0,
    usd          REAL NOT NULL DEFAULT 0.0,
    recorded_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workspace_usage ON workspace_usage(workspace_id, date);
"""


def _apply_wal(conn: sqlite3.Connection) -> None:
    """Enable WAL mode for better concurrent read/write performance."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")


class SqliteStore:
    """Synchronous SQLite store. Async wrappers run in a dedicated ThreadPoolExecutor.

    A single-worker executor is used because SQLite only supports one writer
    at a time. This avoids blocking the asyncio event loop on DB operations.
    """

    def __init__(self, db_path: str | Path = "horizonx.db"):
        self.db_path = str(db_path)
        # Single worker — SQLite serialises writes; more workers add contention
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sqlite")
        self._sync_init_schema()

    def _sync_init_schema(self) -> None:
        with self._conn() as c:
            _apply_wal(c)
            c.executescript(SCHEMA)
            # Migration: add assigned_to_session to existing DBs that predate HX-20
            try:
                c.execute("ALTER TABLE goals ADD COLUMN assigned_to_session TEXT")
            except Exception:
                pass  # column already exists
            # Migration: add validators / inherit_validators to DBs that predate GE-03
            try:
                c.execute("ALTER TABLE goals ADD COLUMN validators TEXT")
            except Exception:
                pass  # column already exists
            try:
                c.execute("ALTER TABLE goals ADD COLUMN inherit_validators INTEGER NOT NULL DEFAULT 1")
            except Exception:
                pass  # column already exists

    async def _run_sync(self, fn: Callable[..., _T], *args: Any) -> _T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn, *args)

    async def close(self) -> None:
        self._executor.shutdown(wait=True)

    @contextmanager
    def _conn(self):  # type: ignore[no-untyped-def]
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

    def _sync_save_run(self, run: Run) -> None:
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

    async def save_run(self, run: Run) -> None:
        return await self._run_sync(self._sync_save_run, run)

    def _sync_load_run(self, run_id: str) -> Run:
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

    async def load_run(self, run_id: str) -> Run:
        return await self._run_sync(self._sync_load_run, run_id)

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def _sync_save_session(self, s: Session) -> None:
        with self._conn() as c:
            c.execute(
                """\
                INSERT INTO sessions (id, run_id, sequence_index, target_goal_id, status,
                                      started_at, completed_at, steps_count, housekeeping_steps,
                                      tokens_used, agent_session_id, handoff_summary_path)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    completed_at=excluded.completed_at,
                    steps_count=excluded.steps_count,
                    housekeeping_steps=excluded.housekeeping_steps,
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
                    s.housekeeping_steps,
                    s.tokens_used,
                    s.agent_session_id,
                    str(s.handoff_summary_path) if s.handoff_summary_path else None,
                ),
            )

    async def save_session(self, s: Session) -> None:
        return await self._run_sync(self._sync_save_session, s)

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    def _sync_save_step(self, step: Step) -> None:
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

    async def save_step(self, step: Step) -> None:
        return await self._run_sync(self._sync_save_step, step)

    def _sync_recent_steps(self, session_id: str, n: int) -> list[Step]:
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

    async def recent_steps(self, session_id: str, n: int) -> list[Step]:
        return await self._run_sync(self._sync_recent_steps, session_id, n)

    def _sync_recent_validator_scores(self, run_id: str, n: int) -> list[float]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT score FROM validations WHERE run_id=? AND score IS NOT NULL ORDER BY started_at DESC LIMIT ?",
                (run_id, n),
            ).fetchall()
        return list(reversed([r["score"] for r in rows]))

    async def recent_validator_scores(self, run_id: str, n: int) -> list[float]:
        return await self._run_sync(self._sync_recent_validator_scores, run_id, n)

    # ------------------------------------------------------------------
    # Goals
    # ------------------------------------------------------------------

    def _sync_save_goal(self, run_id: str, g: GoalNode) -> None:
        with self._conn() as c:
            c.execute(
                """\
                INSERT INTO goals (id, run_id, parent_id, name, description, verification_criteria,
                                   status, attempts, notes, last_updated_at, last_updated_by_session,
                                   assigned_to_session, validators, inherit_validators)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    attempts=excluded.attempts,
                    notes=excluded.notes,
                    last_updated_at=excluded.last_updated_at,
                    last_updated_by_session=excluded.last_updated_by_session,
                    assigned_to_session=excluded.assigned_to_session,
                    validators=excluded.validators,
                    inherit_validators=excluded.inherit_validators
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
                    g.assigned_to_session,
                    json.dumps([v.model_dump(mode="json") for v in g.validators]),
                    1 if g.inherit_validators else 0,
                ),
            )

    async def save_goal(self, run_id: str, g: GoalNode) -> None:
        return await self._run_sync(self._sync_save_goal, run_id, g)

    def _sync_claim_goal(self, run_id: str, goal_id: str, session_id: str) -> bool:
        """Atomically claim a PENDING goal for a session.

        Uses BEGIN IMMEDIATE to prevent two parallel agents double-claiming the
        same leaf. Returns True if this session won the claim, False if another
        session already claimed or the goal is no longer PENDING.
        """
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE goals SET assigned_to_session=?, status='in_progress' "
                "WHERE id=? AND run_id=? AND status='pending' AND assigned_to_session IS NULL",
                (session_id, goal_id, run_id),
            )
            claimed = cur.rowcount == 1
            conn.commit()
            return claimed
        except Exception:
            conn.rollback()
            return False
        finally:
            conn.close()

    async def claim_goal(self, run_id: str, goal_id: str, session_id: str) -> bool:
        return await self._run_sync(self._sync_claim_goal, run_id, goal_id, session_id)

    def _sync_release_goal(self, run_id: str, goal_id: str) -> None:
        """Clear the assignment when a goal completes or fails."""
        with self._conn() as c:
            c.execute(
                "UPDATE goals SET assigned_to_session=NULL WHERE id=? AND run_id=?",
                (goal_id, run_id),
            )

    async def release_goal(self, run_id: str, goal_id: str) -> None:
        return await self._run_sync(self._sync_release_goal, run_id, goal_id)

    def _sync_load_goal(self, run_id: str, goal_id: str) -> GoalNode | None:
        from horizonx.core.types import GoalStatus

        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM goals WHERE run_id=? AND id=?", (run_id, goal_id)
            ).fetchone()
        if not row:
            return None
        row_keys = row.keys()
        raw_validators = row["validators"] if "validators" in row_keys else None
        raw_inherit = row["inherit_validators"] if "inherit_validators" in row_keys else 1
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
            assigned_to_session=row["assigned_to_session"] if "assigned_to_session" in row_keys else None,
            validators=[ValidatorConfig(**v) for v in json.loads(raw_validators or "[]")],
            inherit_validators=bool(raw_inherit) if raw_inherit is not None else True,
        )

    async def load_goal(self, run_id: str, goal_id: str) -> GoalNode | None:
        return await self._run_sync(self._sync_load_goal, run_id, goal_id)

    # ------------------------------------------------------------------
    # Validations / HITL / Spin
    # ------------------------------------------------------------------

    def _sync_save_validation(
        self, run: Run, session: Session | None, decision: GateDecision
    ) -> None:
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

    async def save_validation(self, run: Run, session: Session | None, decision: GateDecision) -> None:
        return await self._run_sync(self._sync_save_validation, run, session, decision)

    def _sync_save_spin_report(self, session: Session, report: SpinReport) -> None:
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

    async def save_spin_report(self, session: Session, report: SpinReport) -> None:
        return await self._run_sync(self._sync_save_spin_report, session, report)

    def _sync_list_runs(self, limit: int) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, status, started_at, completed_at FROM runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    async def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return await self._run_sync(self._sync_list_runs, limit)

    def _sync_list_run_summaries(
        self, limit: int, status: str | None
    ) -> list[dict[str, Any]]:
        with self._conn() as c:
            if status:
                rows = c.execute(
                    "SELECT id, parent_run_id, status, started_at, completed_at, "
                    "current_session_id, task_snapshot, cumulative FROM runs "
                    "WHERE status=? ORDER BY started_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT id, parent_run_id, status, started_at, completed_at, "
                    "current_session_id, task_snapshot, cumulative FROM runs "
                    "ORDER BY started_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        result = []
        for row in rows:
            task_data = json.loads(row["task_snapshot"])
            cumulative_data = json.loads(row["cumulative"] or "{}")
            result.append({
                "id": row["id"],
                "parent_run_id": row["parent_run_id"],
                "status": row["status"],
                "started_at": row["started_at"],
                "completed_at": row["completed_at"],
                "current_session_id": row["current_session_id"],
                "task_id": task_data.get("id"),
                "task_name": task_data.get("name"),
                "tags": task_data.get("tags", []),
                "sessions_count": cumulative_data.get("sessions_count", 0),
                "steps_count": cumulative_data.get("steps_count", 0),
                "usd": cumulative_data.get("usd", 0.0),
                "tokens_in": cumulative_data.get("tokens_in", 0),
                "tokens_out": cumulative_data.get("tokens_out", 0),
                "wall_seconds": cumulative_data.get("wall_seconds", 0.0),
            })
        return result

    async def list_run_summaries(
        self, limit: int = 50, status: str | None = None
    ) -> list[dict[str, Any]]:
        return await self._run_sync(self._sync_list_run_summaries, limit, status)

    # ------------------------------------------------------------------
    # Extended queries for dashboard
    # ------------------------------------------------------------------

    def _sync_list_sessions(self, run_id: str) -> list[Session]:
        from horizonx.core.types import SessionStatus

        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM sessions WHERE run_id=? ORDER BY sequence_index",
                (run_id,),
            ).fetchall()
        out = []
        for row in rows:
            out.append(
                Session(
                    id=row["id"],
                    run_id=row["run_id"],
                    sequence_index=row["sequence_index"],
                    target_goal_id=row["target_goal_id"],
                    status=SessionStatus(row["status"]),
                    started_at=row["started_at"],
                    completed_at=row["completed_at"],
                    steps_count=row["steps_count"],
                    tokens_used=row["tokens_used"],
                    agent_session_id=row["agent_session_id"],
                    handoff_summary_path=row["handoff_summary_path"],
                )
            )
        return out

    async def list_sessions(self, run_id: str) -> list[Session]:
        return await self._run_sync(self._sync_list_sessions, run_id)

    def _sync_list_steps(
        self,
        session_id: str,
        limit: int,
        after_sequence: int | None,
    ) -> list[Step]:
        from horizonx.core.types import StepType

        with self._conn() as c:
            if after_sequence is not None:
                rows = c.execute(
                    "SELECT * FROM steps WHERE session_id=? AND sequence>? "
                    "ORDER BY sequence LIMIT ?",
                    (session_id, after_sequence, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM steps WHERE session_id=? ORDER BY sequence LIMIT ?",
                    (session_id, limit),
                ).fetchall()
        return [
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
            for row in rows
        ]

    async def list_steps(
        self,
        session_id: str,
        limit: int = 500,
        after_sequence: int | None = None,
    ) -> list[Step]:
        return await self._run_sync(self._sync_list_steps, session_id, limit, after_sequence)

    def _sync_list_goals(self, run_id: str) -> list[GoalNode]:
        from horizonx.core.types import GoalStatus

        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM goals WHERE run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()
        nodes = []
        for row in rows:
            row_keys = row.keys()
            raw_validators = row["validators"] if "validators" in row_keys else None
            raw_inherit = row["inherit_validators"] if "inherit_validators" in row_keys else 1
            nodes.append(
                GoalNode(
                    id=row["id"],
                    parent_id=row["parent_id"],
                    name=row["name"],
                    description=row["description"],
                    verification_criteria=json.loads(row["verification_criteria"] or "[]"),
                    status=GoalStatus(row["status"]),
                    attempts=row["attempts"],
                    notes=row["notes"] or "",
                    last_updated_at=row["last_updated_at"],
                    last_updated_by_session=row["last_updated_by_session"],
                    assigned_to_session=row["assigned_to_session"],
                    validators=[ValidatorConfig(**v) for v in json.loads(raw_validators or "[]")],
                    inherit_validators=bool(raw_inherit) if raw_inherit is not None else True,
                )
            )
        # Reconstruct children from parent_id relationships
        id_to_node = {n.id: n for n in nodes}
        for node in nodes:
            if node.parent_id and node.parent_id in id_to_node:
                parent = id_to_node[node.parent_id]
                if node.id not in parent.children:
                    parent.children.append(node.id)
        return nodes

    async def list_goals(self, run_id: str) -> list[GoalNode]:
        return await self._run_sync(self._sync_list_goals, run_id)

    def _sync_list_validations(self, run_id: str) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM validations WHERE run_id=? ORDER BY started_at",
                (run_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    async def list_validations(self, run_id: str) -> list[dict[str, Any]]:
        return await self._run_sync(self._sync_list_validations, run_id)

    def _sync_save_hitl_event(
        self,
        run_id: str,
        trigger: str,
        context: dict[str, Any],
        hitl_id: str | None,
    ) -> str:
        from uuid import uuid4

        event_id = hitl_id or str(uuid4())
        with self._conn() as c:
            c.execute(
                "INSERT INTO hitl_events (id, run_id, triggered_at, trigger, context) "
                "VALUES (?,?,datetime('now'),?,?)",
                (event_id, run_id, trigger, json.dumps(context, default=str)),
            )
        return event_id

    async def save_hitl_event(
        self,
        run_id: str,
        trigger: str,
        context: dict[str, Any],
        hitl_id: str | None = None,
    ) -> str:
        return await self._run_sync(self._sync_save_hitl_event, run_id, trigger, context, hitl_id)

    def _sync_update_hitl_event(
        self,
        event_id: str,
        action: str,
        operator: str | None,
        instruction: str,
    ) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE hitl_events SET resolved_at=datetime('now'), decision=?, operator=?, "
                "instruction=? WHERE id=?",
                (action, operator, instruction, event_id),
            )

    async def update_hitl_event(
        self,
        event_id: str,
        action: str,
        operator: str | None,
        instruction: str,
    ) -> None:
        return await self._run_sync(self._sync_update_hitl_event, event_id, action, operator, instruction)

    def _sync_list_hitl_events(self, run_id: str) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM hitl_events WHERE run_id=? ORDER BY triggered_at",
                (run_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    async def list_hitl_events(self, run_id: str) -> list[dict[str, Any]]:
        return await self._run_sync(self._sync_list_hitl_events, run_id)

    def _sync_list_spin_reports(self, run_id: str) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT sr.* FROM spin_reports sr "
                "JOIN sessions s ON s.id = sr.session_id "
                "WHERE s.run_id=? ORDER BY sr.detected_at",
                (run_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    async def list_spin_reports(self, run_id: str) -> list[dict[str, Any]]:
        return await self._run_sync(self._sync_list_spin_reports, run_id)

    # ------------------------------------------------------------------
    # Pending runs (crash recovery for dashboard-launched runs)
    # ------------------------------------------------------------------

    def _sync_save_pending_run(self, run_id: str, task_json: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO pending_runs (run_id, task_json, status) VALUES (?, ?, 'pending')",
                (run_id, task_json),
            )

    def _sync_mark_pending_run_started(self, run_id: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE pending_runs SET status='started' WHERE run_id=?", (run_id,))

    def _sync_delete_pending_run(self, run_id: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM pending_runs WHERE run_id=?", (run_id,))

    def _sync_list_pending_runs(self) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT run_id, task_json FROM pending_runs WHERE status='pending'"
            ).fetchall()
        return [{"run_id": r["run_id"], "task_json": r["task_json"]} for r in rows]

    async def save_pending_run(self, run_id: str, task_json: str) -> None:
        return await self._run_sync(self._sync_save_pending_run, run_id, task_json)

    async def mark_pending_run_started(self, run_id: str) -> None:
        return await self._run_sync(self._sync_mark_pending_run_started, run_id)

    async def delete_pending_run(self, run_id: str) -> None:
        return await self._run_sync(self._sync_delete_pending_run, run_id)

    async def list_pending_runs(self) -> list[dict[str, Any]]:
        return await self._run_sync(self._sync_list_pending_runs)

    # ------------------------------------------------------------------
    # Workspace usage (cross-run daily budget tracking)
    # ------------------------------------------------------------------

    def _sync_record_workspace_usage(
        self, workspace_id: str, run_id: str,
        tokens_in: int, tokens_out: int, usd: float,
    ) -> None:
        import uuid
        from datetime import date
        with self._conn() as c:
            c.execute(
                "INSERT INTO workspace_usage "
                "(id, workspace_id, run_id, date, tokens_in, tokens_out, usd, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
                (str(uuid.uuid4()), workspace_id, run_id, str(date.today()),
                 tokens_in, tokens_out, usd),
            )

    def _sync_workspace_daily_usd(self, workspace_id: str) -> float:
        from datetime import date
        with self._conn() as c:
            row = c.execute(
                "SELECT COALESCE(SUM(usd), 0.0) FROM workspace_usage "
                "WHERE workspace_id=? AND date=?",
                (workspace_id, str(date.today())),
            ).fetchone()
        return float(row[0]) if row else 0.0

    async def record_workspace_usage(
        self, workspace_id: str, run_id: str,
        tokens_in: int, tokens_out: int, usd: float,
    ) -> None:
        return await self._run_sync(
            self._sync_record_workspace_usage,
            workspace_id, run_id, tokens_in, tokens_out, usd,
        )

    async def workspace_daily_usd(self, workspace_id: str) -> float:
        return await self._run_sync(self._sync_workspace_daily_usd, workspace_id)
