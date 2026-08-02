"""SQLite store. Persists Runs, Sessions, Steps, Goals, Validations, HITL events.

See docs/LONG_HORIZON_AGENT.md §17 for the schema.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import sqlite3
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from horizonx.core.types import (
    TERMINAL_RUN_STATUSES,
    GateDecision,
    GoalNode,
    GoalStatus,
    Run,
    RunStatus,
    Session,
    SpinReport,
    Step,
    ValidatorConfig,
    utcnow,
)
from horizonx.storage.migrations import (
    prepare_schema,
    read_schema_version,
    record_current_schema,
)

_T = TypeVar("_T")

if TYPE_CHECKING:
    from horizonx.core.goal_graph import GoalGraph


class StoreError(RuntimeError):
    """Base class for typed operational store failures."""


class StoreBusyError(StoreError):
    """Raised after SQLite remains locked for the configured busy timeout."""


class GoalTransitionError(StoreError):
    """Raised when a requested goal status transition is not allowed."""


class GoalVersionConflict(StoreError):
    """Raised when optimistic goal version preconditions do not match."""


_ALLOWED_GOAL_TRANSITIONS: dict[GoalStatus, frozenset[GoalStatus]] = {
    GoalStatus.PENDING: frozenset(
        {GoalStatus.IN_PROGRESS, GoalStatus.BLOCKED, GoalStatus.SKIPPED}
    ),
    GoalStatus.IN_PROGRESS: frozenset(
        {GoalStatus.PENDING, GoalStatus.DONE, GoalStatus.FAILED, GoalStatus.BLOCKED}
    ),
    GoalStatus.BLOCKED: frozenset({GoalStatus.PENDING, GoalStatus.FAILED}),
    GoalStatus.DONE: frozenset(),
    GoalStatus.FAILED: frozenset(),
    GoalStatus.SKIPPED: frozenset(),
}


def _is_busy_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


_NETWORK_FILESYSTEMS = frozenset(
    {
        "9p",
        "afpfs",
        "cifs",
        "davfs",
        "fuse.sshfs",
        "nfs",
        "nfs4",
        "smbfs",
    }
)


def _existing_parent(path: Path) -> Path:
    candidate = path.expanduser().resolve(strict=False)
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _unescape_mount_path(value: str) -> str:
    for escaped, character in (
        ("\\040", " "),
        ("\\011", "\t"),
        ("\\012", "\n"),
        ("\\134", "\\"),
    ):
        value = value.replace(escaped, character)
    return value


def _filesystem_type(path: Path) -> str | None:
    """Return the containing filesystem type when the platform exposes it."""
    existing = _existing_parent(path)
    system = platform.system()
    if system == "Linux":
        try:
            entries: list[tuple[int, str]] = []
            with open("/proc/self/mountinfo", encoding="utf-8") as mountinfo:
                for line in mountinfo:
                    left, separator, right = line.partition(" - ")
                    if not separator:
                        continue
                    fields = left.split()
                    details = right.split()
                    if len(fields) < 5 or not details:
                        continue
                    mount_point = _unescape_mount_path(fields[4])
                    try:
                        common = os.path.commonpath((str(existing), mount_point))
                    except ValueError:
                        continue
                    if common == mount_point:
                        entries.append((len(mount_point), details[0]))
            return max(entries)[1] if entries else None
        except OSError:
            return None
    if system == "Darwin":
        try:
            result = subprocess.run(
                ["stat", "-f", "%T", str(existing)],
                check=True,
                capture_output=True,
                text=True,
            )
            return result.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            return None
    return None


def _assert_local_database_path(db_path: str | Path) -> None:
    raw = str(db_path)
    if raw == ":memory:":
        raise StoreError(
            "SqliteStore requires a file-backed database for durable async operations"
        )
    if raw.startswith(("//", "\\\\", "smb://", "nfs://", "afp://")):
        raise StoreError("SQLite database must be on a local filesystem")
    filesystem = _filesystem_type(Path(raw))
    if filesystem is not None and filesystem.lower() in _NETWORK_FILESYSTEMS:
        raise StoreError(
            "SQLite database must be on a local filesystem; "
            f"detected {filesystem}"
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
    run_id                   TEXT NOT NULL,
    id                       TEXT NOT NULL,
    name                     TEXT NOT NULL,
    description              TEXT NOT NULL,
    verification_criteria    TEXT NOT NULL,
    status                   TEXT NOT NULL,
    attempts                 INTEGER NOT NULL DEFAULT 0,
    max_attempts             INTEGER NOT NULL DEFAULT 3,
    progress_pct             REAL NOT NULL DEFAULT 0.0,
    version                  INTEGER NOT NULL DEFAULT 0,
    notes                    TEXT,
    last_updated_at          TEXT NOT NULL,
    last_updated_by_session  TEXT,
    assigned_to_session      TEXT,
    validators               TEXT,
    inherit_validators       INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (run_id, id)
);
CREATE INDEX IF NOT EXISTS idx_goals_run ON goals(run_id, status);

CREATE TABLE IF NOT EXISTS goal_edges (
    run_id       TEXT NOT NULL,
    from_goal_id TEXT NOT NULL,
    to_goal_id   TEXT NOT NULL,
    edge_type    TEXT NOT NULL CHECK (edge_type IN ('parent', 'dependency')),
    position     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, from_goal_id, to_goal_id, edge_type),
    FOREIGN KEY (run_id, from_goal_id) REFERENCES goals(run_id, id) ON DELETE CASCADE,
    FOREIGN KEY (run_id, to_goal_id) REFERENCES goals(run_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_goal_edges_to
    ON goal_edges(run_id, to_goal_id, edge_type);

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


def _configure_connection(conn: sqlite3.Connection, busy_timeout_ms: int) -> None:
    """Apply the required safety and concurrency policy to every connection."""
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")


class SqliteStore:
    """Synchronous SQLite store. Async wrappers run in a dedicated ThreadPoolExecutor.

    A single-worker executor is used because SQLite only supports one writer
    at a time. This avoids blocking the asyncio event loop on DB operations.
    """

    def __init__(self, db_path: str | Path = "horizonx.db", *, busy_timeout_ms: int = 5000):
        if not isinstance(busy_timeout_ms, int) or busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must be a non-negative integer")
        _assert_local_database_path(db_path)
        self.db_path = str(db_path)
        self.busy_timeout_ms = busy_timeout_ms
        # Single worker — SQLite serialises writes; more workers add contention
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sqlite")
        self._sync_init_schema()

    def _sync_init_schema(self) -> None:
        with self._conn() as c:
            prepare_schema(c)
            c.executescript(SCHEMA)
            record_current_schema(c)

    async def _run_sync(self, fn: Callable[..., _T], *args: Any) -> _T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn, *args)

    async def close(self) -> None:
        self._executor.shutdown(wait=True)

    def _sync_schema_version(self) -> int:
        with self._conn() as c:
            return read_schema_version(c)

    async def schema_version(self) -> int:
        return await self._run_sync(self._sync_schema_version)

    def _sync_connection_settings(self) -> dict[str, int | str]:
        with self._conn() as c:
            return {
                "foreign_keys": int(c.execute("PRAGMA foreign_keys").fetchone()[0]),
                "busy_timeout": int(c.execute("PRAGMA busy_timeout").fetchone()[0]),
                "journal_mode": str(c.execute("PRAGMA journal_mode").fetchone()[0]),
                "synchronous": int(c.execute("PRAGMA synchronous").fetchone()[0]),
            }

    async def connection_settings(self) -> dict[str, int | str]:
        """Report the effective safety policy of a fresh store connection."""
        return await self._run_sync(self._sync_connection_settings)

    def _sync_integrity_check(self) -> list[str]:
        with self._conn() as c:
            rows = c.execute("PRAGMA integrity_check").fetchall()
        return [str(row[0]) for row in rows]

    async def integrity_check(self) -> list[str]:
        return await self._run_sync(self._sync_integrity_check)

    def _sync_checkpoint(self) -> tuple[int, int, int]:
        with self._conn() as c:
            row = c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if row is None:  # pragma: no cover - SQLite always returns one row
            raise StoreError("WAL checkpoint returned no result")
        return int(row[0]), int(row[1]), int(row[2])

    async def checkpoint(self) -> tuple[int, int, int]:
        return await self._run_sync(self._sync_checkpoint)

    def _sync_state_digest(self) -> str:
        with self._conn() as c:
            tables = [
                row["name"]
                for row in c.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                ).fetchall()
            ]
            state: dict[str, list[str]] = {}
            for table in tables:
                quoted_table = table.replace('"', '""')
                rows = c.execute(f'SELECT * FROM "{quoted_table}"').fetchall()
                state[table] = sorted(
                    json.dumps(dict(row), sort_keys=True, default=str) for row in rows
                )
        payload = json.dumps(state, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    async def state_digest(self) -> str:
        """Return a deterministic digest of logical user-table contents."""
        return await self._run_sync(self._sync_state_digest)

    def _sync_backup(self, destination: Path) -> None:
        if destination.resolve(strict=False) == Path(self.db_path).resolve(strict=False):
            raise StoreError("backup destination must be a different path")
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect(self.db_path, timeout=self.busy_timeout_ms / 1000)
        target = sqlite3.connect(destination)
        try:
            _configure_connection(source, self.busy_timeout_ms)
            _configure_connection(target, self.busy_timeout_ms)
            source.backup(target)
            target.commit()
            result = target.execute("PRAGMA integrity_check").fetchall()
            if [str(row[0]) for row in result] != ["ok"]:
                raise StoreError(f"backup integrity check failed: {result}")
        finally:
            target.close()
            source.close()

    async def backup(self, destination: str | Path) -> Path:
        path = Path(destination)
        await self._run_sync(self._sync_backup, path)
        return path

    def _sync_restore(self, source_path: Path) -> None:
        if source_path.resolve(strict=False) == Path(self.db_path).resolve(strict=False):
            raise StoreError("restore source must be a different path")
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        source = sqlite3.connect(source_path)
        target = sqlite3.connect(self.db_path, timeout=self.busy_timeout_ms / 1000)
        try:
            _configure_connection(source, self.busy_timeout_ms)
            _configure_connection(target, self.busy_timeout_ms)
            source_result = source.execute("PRAGMA integrity_check").fetchall()
            if [str(row[0]) for row in source_result] != ["ok"]:
                raise StoreError(f"restore source integrity check failed: {source_result}")
            source.backup(target)
            target.commit()
            prepare_schema(target)
            target.executescript(SCHEMA)
            record_current_schema(target)
            target.commit()
            target_result = target.execute("PRAGMA integrity_check").fetchall()
            if [str(row[0]) for row in target_result] != ["ok"]:
                raise StoreError(f"restored database integrity check failed: {target_result}")
        finally:
            target.close()
            source.close()

    async def restore(self, source: str | Path) -> None:
        await self._run_sync(self._sync_restore, Path(source))

    @contextmanager
    def _conn(self):  # type: ignore[no-untyped-def]
        conn = sqlite3.connect(self.db_path, timeout=self.busy_timeout_ms / 1000)
        conn.row_factory = sqlite3.Row
        try:
            _configure_connection(conn, self.busy_timeout_ms)
            yield conn
            conn.commit()
        except sqlite3.OperationalError as exc:
            conn.rollback()
            if _is_busy_error(exc):
                raise StoreBusyError(
                    f"database remained busy for {self.busy_timeout_ms}ms"
                ) from exc
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    def _sync_save_run(self, run: Run) -> None:
        if run.status in TERMINAL_RUN_STATUSES and run.completed_at is None:
            run.completed_at = utcnow()
        with self._conn() as c:
            c.execute(
                """\
                INSERT INTO runs (id, parent_run_id, task_snapshot, status, workspace_path,
                                 started_at, completed_at, current_session_id, goal_graph_root, cumulative)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    status=CASE
                        WHEN runs.status IN ('completed', 'failed', 'aborted',
                                             'timed_out', 'budget_exceeded')
                        THEN runs.status ELSE excluded.status END,
                    completed_at=CASE
                        WHEN runs.status IN ('completed', 'failed', 'aborted',
                                             'timed_out', 'budget_exceeded')
                        THEN runs.completed_at ELSE excluded.completed_at END,
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

    def _sync_transition_run(self, run_id: str, to_status: RunStatus) -> Run:
        if to_status not in TERMINAL_RUN_STATUSES:
            raise StoreError(f"run transition target must be terminal: {to_status.value}")
        with self._conn() as c:
            row = c.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"run not found: {run_id}")
            current = RunStatus(row["status"])
            if current not in TERMINAL_RUN_STATUSES:
                c.execute(
                    "UPDATE runs SET status=?, completed_at=COALESCE(completed_at, ?) "
                    "WHERE id=? AND status NOT IN "
                    "('completed', 'failed', 'aborted', 'timed_out', 'budget_exceeded')",
                    (to_status.value, utcnow().isoformat(), run_id),
                )
        return self._sync_load_run(run_id)

    async def transition_run(self, run_id: str, to_status: RunStatus) -> Run:
        """Set a terminal status once; later terminal writes preserve the first result."""
        return await self._run_sync(self._sync_transition_run, run_id, to_status)

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
            started_at=row["started_at"],
            completed_at=row["completed_at"],
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

    def _upsert_goal_row(
        self, c: sqlite3.Connection, run_id: str, g: GoalNode
    ) -> None:
        c.execute(
            """\
                INSERT INTO goals (run_id, id, name, description, verification_criteria,
                                   status, attempts, max_attempts, progress_pct, version, notes,
                                   last_updated_at, last_updated_by_session, assigned_to_session,
                                   validators, inherit_validators)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id, id) DO UPDATE SET
                    name=excluded.name,
                    description=excluded.description,
                    verification_criteria=excluded.verification_criteria,
                    status=excluded.status,
                    attempts=excluded.attempts,
                    max_attempts=excluded.max_attempts,
                    progress_pct=excluded.progress_pct,
                    version=excluded.version,
                    notes=excluded.notes,
                    last_updated_at=excluded.last_updated_at,
                    last_updated_by_session=excluded.last_updated_by_session,
                    assigned_to_session=excluded.assigned_to_session,
                    validators=excluded.validators,
                    inherit_validators=excluded.inherit_validators
            """,
            (
                run_id,
                g.id,
                g.name,
                g.description,
                json.dumps(g.verification_criteria),
                g.status.value,
                g.attempts,
                g.max_attempts,
                g.progress_pct,
                g.version,
                g.notes,
                g.last_updated_at.isoformat(),
                g.last_updated_by_session,
                g.assigned_to_session,
                json.dumps([v.model_dump(mode="json") for v in g.validators]),
                1 if g.inherit_validators else 0,
            ),
        )

    def _replace_goal_edges(
        self, c: sqlite3.Connection, run_id: str, g: GoalNode
    ) -> None:
        c.execute(
            "DELETE FROM goal_edges WHERE run_id=? AND to_goal_id=? "
            "AND edge_type IN ('parent', 'dependency')",
            (run_id, g.id),
        )
        if g.parent_id is not None:
            c.execute(
                "INSERT INTO goal_edges (run_id, from_goal_id, to_goal_id, edge_type) "
                "VALUES (?, ?, ?, 'parent')",
                (run_id, g.parent_id, g.id),
            )
        c.executemany(
            "INSERT INTO goal_edges "
            "(run_id, from_goal_id, to_goal_id, edge_type, position) "
            "VALUES (?, ?, ?, 'dependency', ?)",
            [
                (run_id, dependency_id, g.id, position)
                for position, dependency_id in enumerate(g.depends_on)
            ],
        )

    def _sync_save_goal(self, run_id: str, g: GoalNode) -> None:
        with self._conn() as c:
            self._upsert_goal_row(c, run_id, g)
            self._replace_goal_edges(c, run_id, g)

    async def save_goal(self, run_id: str, g: GoalNode) -> None:
        return await self._run_sync(self._sync_save_goal, run_id, g)

    def _replace_graph_rows(
        self, c: sqlite3.Connection, run_id: str, graph: GoalGraph
    ) -> None:
        nodes = list(graph.all_nodes())
        c.execute("DELETE FROM goals WHERE run_id=?", (run_id,))
        for node in nodes:
            self._upsert_goal_row(c, run_id, node)
        for node in nodes:
            c.executemany(
                "INSERT INTO goal_edges "
                "(run_id, from_goal_id, to_goal_id, edge_type, position) "
                "VALUES (?, ?, ?, 'parent', ?)",
                [
                    (run_id, node.id, child_id, position)
                    for position, child_id in enumerate(node.children)
                ],
            )
            c.executemany(
                "INSERT INTO goal_edges "
                "(run_id, from_goal_id, to_goal_id, edge_type, position) "
                "VALUES (?, ?, ?, 'dependency', ?)",
                [
                    (run_id, dependency_id, node.id, position)
                    for position, dependency_id in enumerate(node.depends_on)
                ],
            )

    def _sync_create_graph(self, run_id: str, graph: GoalGraph) -> None:
        with self._conn() as c:
            self._replace_graph_rows(c, run_id, graph)

    async def create_graph(self, run_id: str, graph: GoalGraph) -> None:
        """Atomically replace a run's complete graph in the authoritative store."""
        return await self._run_sync(self._sync_create_graph, run_id, graph)

    def _sync_replace_pending_subgraph(self, run_id: str, graph: GoalGraph) -> None:
        with self._conn() as c:
            existing_nodes = self._list_goals_from_connection(c, run_id)
            if not existing_nodes:
                raise KeyError(f"goal graph not found for run: {run_id}")
            candidates = {node.id: node for node in graph.all_nodes()}
            for completed in (
                node for node in existing_nodes if node.status == GoalStatus.DONE
            ):
                candidate = candidates.get(completed.id)
                if (
                    candidate is None
                    or candidate.model_dump(mode="json")
                    != completed.model_dump(mode="json")
                ):
                    raise GoalTransitionError(
                        f"completed goal cannot be removed or rewritten: {completed.id}"
                    )
            self._replace_graph_rows(c, run_id, graph)

    async def replace_pending_subgraph(self, run_id: str, graph: GoalGraph) -> None:
        """Replace unfinished planning while preserving completed nodes exactly."""
        return await self._run_sync(self._sync_replace_pending_subgraph, run_id, graph)

    def _sync_load_graph(self, run_id: str) -> GoalGraph | None:
        from horizonx.core.goal_graph import GoalGraph

        nodes = self._sync_list_goals(run_id)
        if not nodes:
            return None
        return GoalGraph({node.id: node for node in nodes})

    async def load_graph(self, run_id: str) -> GoalGraph | None:
        return await self._run_sync(self._sync_load_graph, run_id)

    def _sync_ensure_goal_projection(self, run_id: str, path: Path) -> bool:
        from horizonx.core.goal_graph import GoalGraph, GoalGraphError

        graph = self._sync_load_graph(run_id)
        if graph is None:
            raise KeyError(f"goal graph not found for run: {run_id}")
        try:
            projected = GoalGraph.load(path)
            current = {
                node.id: node.model_dump(mode="json") for node in graph.all_nodes()
            }
            existing = {
                node.id: node.model_dump(mode="json") for node in projected.all_nodes()
            }
            if existing == current:
                return False
        except (FileNotFoundError, json.JSONDecodeError, KeyError, GoalGraphError, ValueError):
            pass
        graph.save(path)
        return True

    async def ensure_goal_projection(self, run_id: str, path: Path) -> bool:
        """Regenerate a missing, corrupt, or stale JSON projection from SQLite."""
        return await self._run_sync(self._sync_ensure_goal_projection, run_id, path)

    def _sync_transition_goal(
        self,
        run_id: str,
        goal_id: str,
        expected_version: int,
        to_status: GoalStatus,
        session_id: str | None,
    ) -> GoalNode:
        with self._conn() as c:
            row = c.execute(
                "SELECT status, version FROM goals WHERE run_id=? AND id=?",
                (run_id, goal_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"goal not found: {run_id}/{goal_id}")
            current_status = GoalStatus(row["status"])
            current_version = int(row["version"])
            if current_version != expected_version:
                raise GoalVersionConflict(
                    f"goal version conflict for {run_id}/{goal_id}: "
                    f"expected {expected_version}, found {current_version}"
                )
            if to_status not in _ALLOWED_GOAL_TRANSITIONS[current_status]:
                raise GoalTransitionError(
                    f"invalid goal transition for {run_id}/{goal_id}: "
                    f"{current_status.value} -> {to_status.value}"
                )

            entering_progress = to_status == GoalStatus.IN_PROGRESS
            leaving_progress = to_status in {
                GoalStatus.PENDING,
                GoalStatus.DONE,
                GoalStatus.FAILED,
                GoalStatus.BLOCKED,
                GoalStatus.SKIPPED,
            }
            cur = c.execute(
                """
                UPDATE goals
                SET status=?,
                    version=version + 1,
                    attempts=attempts + ?,
                    progress_pct=CASE WHEN ? = 'done' THEN 100.0 ELSE progress_pct END,
                    assigned_to_session=?,
                    last_updated_at=?,
                    last_updated_by_session=?
                WHERE run_id=? AND id=? AND version=?
                """,
                (
                    to_status.value,
                    1 if entering_progress else 0,
                    to_status.value,
                    None if leaving_progress else session_id,
                    utcnow().isoformat(),
                    session_id,
                    run_id,
                    goal_id,
                    expected_version,
                ),
            )
            if cur.rowcount != 1:
                raise GoalVersionConflict(
                    f"goal version changed while transitioning {run_id}/{goal_id}"
                )
        goal = self._sync_load_goal(run_id, goal_id)
        if goal is None:  # pragma: no cover - protected by the transaction above
            raise KeyError(f"goal not found after transition: {run_id}/{goal_id}")
        return goal

    async def transition_goal(
        self,
        run_id: str,
        goal_id: str,
        *,
        expected_version: int,
        to_status: GoalStatus,
        session_id: str | None = None,
    ) -> GoalNode:
        """Apply an allowed optimistic goal transition atomically."""
        return await self._run_sync(
            self._sync_transition_goal,
            run_id,
            goal_id,
            expected_version,
            to_status,
            session_id,
        )

    def _sync_claim_goal(self, run_id: str, goal_id: str, session_id: str) -> bool:
        """Atomically claim a PENDING goal for a session.

        Uses BEGIN IMMEDIATE to prevent two parallel agents double-claiming the
        same leaf. Returns True if this session won the claim, False if another
        session already claimed or the goal is no longer PENDING.
        """
        conn = sqlite3.connect(self.db_path, timeout=self.busy_timeout_ms / 1000)
        _configure_connection(conn, self.busy_timeout_ms)
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE goals SET assigned_to_session=?, status='in_progress', "
                "attempts=attempts + 1, version=version + 1, "
                "last_updated_at=?, last_updated_by_session=? "
                "WHERE id=? AND run_id=? AND status='pending' AND assigned_to_session IS NULL",
                (session_id, utcnow().isoformat(), session_id, goal_id, run_id),
            )
            claimed = cur.rowcount == 1
            conn.commit()
            return claimed
        except sqlite3.OperationalError as exc:
            conn.rollback()
            if _is_busy_error(exc):
                raise StoreBusyError(
                    f"database remained busy for {self.busy_timeout_ms}ms"
                ) from exc
            raise
        except Exception:
            conn.rollback()
            raise
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
            edges = c.execute(
                "SELECT from_goal_id, to_goal_id, edge_type FROM goal_edges "
                "WHERE run_id=? AND (from_goal_id=? OR to_goal_id=?) "
                "ORDER BY edge_type, position",
                (run_id, goal_id, goal_id),
            ).fetchall()
        parent_id = next(
            (
                edge["from_goal_id"]
                for edge in edges
                if edge["edge_type"] == "parent" and edge["to_goal_id"] == goal_id
            ),
            None,
        )
        children = [
            edge["to_goal_id"]
            for edge in edges
            if edge["edge_type"] == "parent" and edge["from_goal_id"] == goal_id
        ]
        depends_on = [
            edge["from_goal_id"]
            for edge in edges
            if edge["edge_type"] == "dependency" and edge["to_goal_id"] == goal_id
        ]
        return GoalNode(
            id=row["id"],
            parent_id=parent_id,
            name=row["name"],
            description=row["description"],
            verification_criteria=json.loads(row["verification_criteria"]),
            status=GoalStatus(row["status"]),
            children=children,
            depends_on=depends_on,
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            progress_pct=row["progress_pct"],
            version=row["version"],
            notes=row["notes"] or "",
            last_updated_at=row["last_updated_at"],
            last_updated_by_session=row["last_updated_by_session"],
            assigned_to_session=row["assigned_to_session"],
            validators=[ValidatorConfig(**v) for v in json.loads(row["validators"] or "[]")],
            inherit_validators=bool(row["inherit_validators"]),
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

    def _list_goals_from_connection(
        self, c: sqlite3.Connection, run_id: str
    ) -> list[GoalNode]:
        from horizonx.core.types import GoalStatus

        rows = c.execute(
            "SELECT * FROM goals WHERE run_id=? ORDER BY id",
            (run_id,),
        ).fetchall()
        edges = c.execute(
            "SELECT from_goal_id, to_goal_id, edge_type FROM goal_edges "
            "WHERE run_id=? ORDER BY edge_type, position",
            (run_id,),
        ).fetchall()
        nodes = []
        for row in rows:
            goal_id = row["id"]
            parent_id = next(
                (
                    edge["from_goal_id"]
                    for edge in edges
                    if edge["edge_type"] == "parent" and edge["to_goal_id"] == goal_id
                ),
                None,
            )
            nodes.append(
                GoalNode(
                    id=goal_id,
                    parent_id=parent_id,
                    name=row["name"],
                    description=row["description"],
                    verification_criteria=json.loads(row["verification_criteria"] or "[]"),
                    status=GoalStatus(row["status"]),
                    children=[
                        edge["to_goal_id"]
                        for edge in edges
                        if edge["edge_type"] == "parent"
                        and edge["from_goal_id"] == goal_id
                    ],
                    depends_on=[
                        edge["from_goal_id"]
                        for edge in edges
                        if edge["edge_type"] == "dependency"
                        and edge["to_goal_id"] == goal_id
                    ],
                    attempts=row["attempts"],
                    max_attempts=row["max_attempts"],
                    progress_pct=row["progress_pct"],
                    version=row["version"],
                    notes=row["notes"] or "",
                    last_updated_at=row["last_updated_at"],
                    last_updated_by_session=row["last_updated_by_session"],
                    assigned_to_session=row["assigned_to_session"],
                    validators=[ValidatorConfig(**v) for v in json.loads(row["validators"] or "[]")],
                    inherit_validators=bool(row["inherit_validators"]),
                )
            )
        return nodes

    def _sync_list_goals(self, run_id: str) -> list[GoalNode]:
        with self._conn() as c:
            return self._list_goals_from_connection(c, run_id)

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
