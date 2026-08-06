"""SQLite store. Persists Runs, Sessions, Steps, Goals, Validations, HITL events.

See docs/LONG_HORIZON_AGENT.md §17 for the schema.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import platform
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from horizonx.core.types import (
    TERMINAL_ATTEMPT_STATUSES,
    TERMINAL_RUN_STATUSES,
    AttemptRecord,
    AttemptStatus,
    GateDecision,
    GoalNode,
    GoalStatus,
    LeaseRecord,
    Run,
    RunStatus,
    Session,
    SpinReport,
    Step,
    ValidatorConfig,
    utcnow,
)
from horizonx.storage.migrations import (
    ensure_additive_schema,
    prepare_schema,
    read_schema_version,
    record_current_schema,
    repair_hitl_requested_events,
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


class OperatorCommandConflict(StoreError):
    """Raised when an idempotency key is reused for different command content."""


class HITLTransitionError(StoreError):
    """Raised when a run is no longer eligible to enter HITL."""

    def __init__(self, run_id: str, status: str) -> None:
        self.run_id = run_id
        self.status = status
        super().__init__(f"run {run_id!r} cannot enter HITL from status {status!r}")


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


@contextmanager
def _database_maintenance_lock(
    path: Path, *, exclusive: bool, timeout_ms: int
) -> Iterator[None]:
    """Coordinate normal DB access with destructive maintenance across processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + timeout_ms / 1000
    locked = False
    backend: Any = None
    try:
        if os.name == "posix":
            backend = importlib.import_module("fcntl")
            operation = backend.LOCK_EX if exclusive else backend.LOCK_SH
            while True:
                try:
                    backend.flock(descriptor, operation | backend.LOCK_NB)
                    locked = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise StoreBusyError(
                            f"database maintenance lock remained busy for {timeout_ms}ms"
                        ) from None
                    time.sleep(0.01)
        elif os.name == "nt":  # pragma: no cover - exercised on Windows
            backend = importlib.import_module("msvcrt")
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            while True:
                try:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    backend.locking(descriptor, backend.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise StoreBusyError(
                            f"database maintenance lock remained busy for {timeout_ms}ms"
                        ) from None
                    time.sleep(0.01)
        yield
    finally:
        if locked and os.name == "posix":
            backend.flock(descriptor, backend.LOCK_UN)
        elif locked and os.name == "nt":  # pragma: no cover
            os.lseek(descriptor, 0, os.SEEK_SET)
            backend.locking(descriptor, backend.LK_UNLCK, 1)
        os.close(descriptor)

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

CREATE TABLE IF NOT EXISTS attempts (
    id                  TEXT PRIMARY KEY,
    lineage_id          TEXT NOT NULL,
    run_id              TEXT NOT NULL,
    goal_id             TEXT,
    session_id          TEXT NOT NULL,
    ordinal             INTEGER NOT NULL,
    status              TEXT NOT NULL,
    provider            TEXT NOT NULL,
    model               TEXT NOT NULL,
    workspace_path      TEXT NOT NULL,
    workspace_snapshot  TEXT NOT NULL DEFAULT '{}',
    provider_session_id TEXT,
    error               TEXT,
    retry_cause         TEXT,
    retry_count         INTEGER NOT NULL DEFAULT 0,
    max_attempts        INTEGER NOT NULL DEFAULT 3,
    next_eligible_at    TEXT,
    started_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    completed_at        TEXT,
    version             INTEGER NOT NULL DEFAULT 0,
    UNIQUE (run_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_attempts_recovery
    ON attempts(run_id, status, ordinal DESC);
CREATE INDEX IF NOT EXISTS idx_attempts_session ON attempts(session_id);

CREATE TABLE IF NOT EXISTS events (
    sequence    INTEGER PRIMARY KEY AUTOINCREMENT,
    id          TEXT NOT NULL UNIQUE,
    type        TEXT NOT NULL,
    run_id      TEXT,
    attempt_id  TEXT,
    session_id  TEXT,
    goal_id     TEXT,
    timestamp   TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_run_sequence ON events(run_id, sequence);
CREATE INDEX IF NOT EXISTS idx_events_attempt_sequence ON events(attempt_id, sequence);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_one_strategy_switch
    ON events(run_id) WHERE type='strategy.switched';

CREATE TABLE IF NOT EXISTS leases (
    resource_id  TEXT PRIMARY KEY,
    owner        TEXT NOT NULL,
    acquired_at  TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    version      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leases_expiry ON leases(expires_at);

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
    duration_ms  INTEGER,
    idempotency_key TEXT
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
    instruction  TEXT,
    request_actor TEXT NOT NULL DEFAULT 'system',
    request_reason TEXT NOT NULL DEFAULT '',
    request_instruction TEXT NOT NULL DEFAULT '',
    reason       TEXT NOT NULL DEFAULT '',
    resolution_idempotency_key TEXT
);

CREATE TABLE IF NOT EXISTS operator_commands (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    attempt_id      TEXT,
    kind            TEXT NOT NULL CHECK (kind IN ('cancel', 'steer', 'decision')),
    actor           TEXT NOT NULL,
    reason          TEXT NOT NULL DEFAULT '',
    instruction     TEXT NOT NULL DEFAULT '',
    payload         TEXT NOT NULL DEFAULT '{}',
    idempotency_key TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    consumed_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_operator_commands_pending
    ON operator_commands(run_id, consumed_at, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_operator_commands_idempotency
    ON operator_commands(run_id, idempotency_key);

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
    usd_known    INTEGER NOT NULL DEFAULT 1,
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
        self._maintenance_lock_path = Path(f"{self.db_path}.lock")
        # Single worker — SQLite serialises writes; more workers add contention
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sqlite")
        with _database_maintenance_lock(
            self._maintenance_lock_path,
            exclusive=False,
            timeout_ms=self.busy_timeout_ms,
        ):
            self._sync_init_schema()

    def _sync_init_schema(self) -> None:
        with self._conn() as c:
            prepare_schema(c)
            c.executescript(SCHEMA)
            ensure_additive_schema(c)
            repair_hitl_requested_events(c)
            record_current_schema(c)

    async def _run_sync(self, fn: Callable[..., _T], *args: Any) -> _T:
        loop = asyncio.get_running_loop()

        def invoke() -> _T:
            with _database_maintenance_lock(
                self._maintenance_lock_path,
                exclusive=False,
                timeout_ms=self.busy_timeout_ms,
            ):
                return fn(*args)

        return await loop.run_in_executor(self._executor, invoke)

    async def _run_sync_exclusive(self, fn: Callable[..., _T], *args: Any) -> _T:
        loop = asyncio.get_running_loop()

        def invoke() -> _T:
            with _database_maintenance_lock(
                self._maintenance_lock_path,
                exclusive=True,
                timeout_ms=self.busy_timeout_ms,
            ):
                return fn(*args)

        return await loop.run_in_executor(self._executor, invoke)

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
        target_path = Path(self.db_path)
        if source_path.resolve(strict=False) == target_path.resolve(strict=False):
            raise StoreError("restore source must be a different path")
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        staged_file = tempfile.NamedTemporaryFile(
            prefix=f".{target_path.name}.restore-",
            suffix=".db",
            dir=target_path.parent,
            delete=False,
        )
        staged_path = Path(staged_file.name)
        staged_file.close()
        try:
            with (
                closing(sqlite3.connect(source_path)) as source,
                closing(sqlite3.connect(staged_path)) as staged,
            ):
                source.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
                _configure_connection(staged, self.busy_timeout_ms)
                source_result = source.execute("PRAGMA integrity_check").fetchall()
                if [str(row[0]) for row in source_result] != ["ok"]:
                    raise StoreError(
                        f"restore source integrity check failed: {source_result}"
                    )
                source.backup(staged)
                staged.commit()
                prepare_schema(staged)
                staged.executescript(SCHEMA)
                ensure_additive_schema(staged)
                record_current_schema(staged)
                staged.commit()
                staged_result = staged.execute("PRAGMA integrity_check").fetchall()
                if [str(row[0]) for row in staged_result] != ["ok"]:
                    raise StoreError(
                        f"restored database integrity check failed: {staged_result}"
                    )
                staged.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                staged.execute("PRAGMA journal_mode=DELETE")
                staged.commit()

            with staged_path.open("rb") as staged_file_handle:
                os.fsync(staged_file_handle.fileno())
            if target_path.exists():
                try:
                    with sqlite3.connect(
                        target_path, timeout=self.busy_timeout_ms / 1000
                    ) as target:
                        _configure_connection(target, self.busy_timeout_ms)
                        checkpoint = target.execute(
                            "PRAGMA wal_checkpoint(TRUNCATE)"
                        ).fetchone()
                        if checkpoint is None or int(checkpoint[0]) != 0:
                            raise StoreBusyError(
                                "restore requires exclusive database access; "
                                "close active readers and writers before retrying"
                            )
                        journal_mode = target.execute(
                            "PRAGMA journal_mode=DELETE"
                        ).fetchone()
                        if journal_mode is None or journal_mode[0] != "delete":
                            raise StoreBusyError(
                                "restore requires exclusive database access; "
                                "close active readers and writers before retrying"
                            )
                except sqlite3.OperationalError as exc:
                    if not _is_busy_error(exc):
                        raise
                    raise StoreBusyError(
                        "restore requires exclusive database access; "
                        "close active readers and writers before retrying"
                    ) from exc
                os.chmod(staged_path, target_path.stat().st_mode & 0o7777)
            for suffix in ("-wal", "-shm"):
                Path(f"{target_path}{suffix}").unlink(missing_ok=True)
            os.replace(staged_path, target_path)
            if os.name == "posix":
                directory_fd = os.open(
                    target_path.parent,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            staged_path.unlink(missing_ok=True)

    async def restore(self, source: str | Path) -> None:
        await self._run_sync_exclusive(self._sync_restore, Path(source))

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
                    workspace_path=excluded.workspace_path,
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
    # Attempts — durable execution identity and retry lineage
    # ------------------------------------------------------------------

    @staticmethod
    def _attempt_from_row(row: sqlite3.Row) -> AttemptRecord:
        return AttemptRecord(
            id=row["id"],
            lineage_id=row["lineage_id"],
            run_id=row["run_id"],
            goal_id=row["goal_id"],
            session_id=row["session_id"],
            ordinal=row["ordinal"],
            status=AttemptStatus(row["status"]),
            provider=row["provider"],
            model=row["model"],
            workspace_path=Path(row["workspace_path"]),
            workspace_snapshot=json.loads(row["workspace_snapshot"] or "{}"),
            provider_session_id=row["provider_session_id"],
            error=row["error"],
            retry_cause=row["retry_cause"],
            retry_count=row["retry_count"],
            max_attempts=row["max_attempts"],
            next_eligible_at=row["next_eligible_at"],
            started_at=row["started_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
            version=row["version"],
        )

    def _sync_create_attempt(self, attempt: AttemptRecord) -> AttemptRecord:
        conn = sqlite3.connect(self.db_path, timeout=self.busy_timeout_ms / 1000)
        conn.row_factory = sqlite3.Row
        _configure_connection(conn, self.busy_timeout_ms)
        try:
            conn.execute("BEGIN IMMEDIATE")
            ordinal = attempt.ordinal
            if ordinal <= 0:
                row = conn.execute(
                    "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM attempts WHERE run_id=?",
                    (attempt.run_id,),
                ).fetchone()
                ordinal = int(row[0])
            retry_count = attempt.retry_count
            max_attempts = attempt.max_attempts
            if attempt.lineage_id != attempt.id and retry_count == 0:
                retry_row = conn.execute(
                    "SELECT COALESCE(MAX(retry_count), -1) + 1, "
                    "COALESCE(MAX(max_attempts), ?) FROM attempts "
                    "WHERE lineage_id=?",
                    (attempt.max_attempts, attempt.lineage_id),
                ).fetchone()
                retry_count = int(retry_row[0])
                max_attempts = int(retry_row[1])
            conn.execute(
                """
                INSERT INTO attempts (
                    id, lineage_id, run_id, goal_id, session_id, ordinal, status,
                    provider, model, workspace_path, workspace_snapshot,
                    provider_session_id, error, retry_cause, retry_count,
                    max_attempts, next_eligible_at, started_at, updated_at,
                    completed_at, version
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    attempt.id,
                    attempt.lineage_id,
                    attempt.run_id,
                    attempt.goal_id,
                    attempt.session_id,
                    ordinal,
                    attempt.status.value,
                    attempt.provider,
                    attempt.model,
                    str(attempt.workspace_path),
                    json.dumps(attempt.workspace_snapshot, default=str),
                    attempt.provider_session_id,
                    attempt.error,
                    attempt.retry_cause,
                    retry_count,
                    max_attempts,
                    attempt.next_eligible_at.isoformat()
                    if attempt.next_eligible_at
                    else None,
                    attempt.started_at.isoformat(),
                    attempt.updated_at.isoformat(),
                    attempt.completed_at.isoformat() if attempt.completed_at else None,
                    attempt.version,
                ),
            )
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
        created = self._sync_load_attempt(attempt.id)
        if created is None:  # pragma: no cover - insert and read share one database
            raise StoreError(f"attempt disappeared after insert: {attempt.id}")
        return created

    async def create_attempt(self, attempt: AttemptRecord) -> AttemptRecord:
        return await self._run_sync(self._sync_create_attempt, attempt)

    def _sync_load_attempt(self, attempt_id: str) -> AttemptRecord | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM attempts WHERE id=?", (attempt_id,)
            ).fetchone()
        return self._attempt_from_row(row) if row else None

    async def load_attempt(self, attempt_id: str) -> AttemptRecord | None:
        return await self._run_sync(self._sync_load_attempt, attempt_id)

    def _sync_list_attempts(self, run_id: str) -> list[AttemptRecord]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM attempts WHERE run_id=? ORDER BY ordinal", (run_id,)
            ).fetchall()
        return [self._attempt_from_row(row) for row in rows]

    async def list_attempts(self, run_id: str) -> list[AttemptRecord]:
        return await self._run_sync(self._sync_list_attempts, run_id)

    def _sync_latest_attempt(self, run_id: str) -> AttemptRecord | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM attempts WHERE run_id=? ORDER BY ordinal DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        return self._attempt_from_row(row) if row else None

    async def latest_attempt(self, run_id: str) -> AttemptRecord | None:
        return await self._run_sync(self._sync_latest_attempt, run_id)

    def _sync_set_attempt_provider_session(
        self, attempt_id: str, provider_session_id: str
    ) -> AttemptRecord:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE attempts SET provider_session_id=?, updated_at=?, version=version+1 "
                "WHERE id=? AND status NOT IN ('completed','failed','interrupted','aborted')",
                (provider_session_id, utcnow().isoformat(), attempt_id),
            )
            if cur.rowcount != 1:
                raise StoreError(f"non-terminal attempt not found: {attempt_id}")
        loaded = self._sync_load_attempt(attempt_id)
        if loaded is None:  # pragma: no cover
            raise StoreError(f"attempt disappeared after update: {attempt_id}")
        return loaded

    async def set_attempt_provider_session(
        self, attempt_id: str, provider_session_id: str
    ) -> AttemptRecord:
        return await self._run_sync(
            self._sync_set_attempt_provider_session, attempt_id, provider_session_id
        )

    def _sync_transition_attempt(
        self,
        attempt_id: str,
        to_status: AttemptStatus,
        error: str | None,
        retry_cause: str | None,
        next_eligible_at: Any,
    ) -> AttemptRecord:
        current = self._sync_load_attempt(attempt_id)
        if current is None:
            raise KeyError(f"attempt not found: {attempt_id}")
        if current.status in TERMINAL_ATTEMPT_STATUSES:
            return current
        completed_at = (
            utcnow().isoformat() if to_status in TERMINAL_ATTEMPT_STATUSES else None
        )
        with self._conn() as c:
            c.execute(
                "UPDATE attempts SET status=?, error=?, retry_cause=?, "
                "next_eligible_at=?, updated_at=?, completed_at=?, version=version+1 "
                "WHERE id=? AND status NOT IN ('completed','failed','interrupted','aborted')",
                (
                    to_status.value,
                    error,
                    retry_cause,
                    next_eligible_at.isoformat() if next_eligible_at else None,
                    utcnow().isoformat(),
                    completed_at,
                    attempt_id,
                ),
            )
        loaded = self._sync_load_attempt(attempt_id)
        if loaded is None:  # pragma: no cover
            raise StoreError(f"attempt disappeared after transition: {attempt_id}")
        return loaded

    async def transition_attempt(
        self,
        attempt_id: str,
        to_status: AttemptStatus,
        *,
        error: str | None = None,
        retry_cause: str | None = None,
        next_eligible_at: Any = None,
    ) -> AttemptRecord:
        return await self._run_sync(
            self._sync_transition_attempt,
            attempt_id,
            to_status,
            error,
            retry_cause,
            next_eligible_at,
        )

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
                    json.dumps(
                        {**step.content, "__horizonx_canonical": step.canonical}, default=str
                    ),
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
            content = json.loads(row["content"])
            out.append(
                Step(
                    id=row["id"],
                    session_id=row["session_id"],
                    sequence=row["sequence"],
                    type=StepType(row["type"]),
                    tool_name=row["tool_name"],
                    content={k: v for k, v in content.items() if k != "__horizonx_canonical"},
                    canonical=content.get("__horizonx_canonical"),
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

    def _sync_recover_goal_claim(
        self, run_id: str, goal_id: str, session_id: str
    ) -> bool:
        """Release only the exact orphaned assignment observed by recovery."""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE goals SET status='pending', assigned_to_session=NULL, "
                "version=version+1, last_updated_at=?, last_updated_by_session=? "
                "WHERE run_id=? AND id=? AND status='in_progress' "
                "AND assigned_to_session=?",
                (
                    utcnow().isoformat(),
                    session_id,
                    run_id,
                    goal_id,
                    session_id,
                ),
            )
        return int(cur.rowcount) == 1

    async def recover_goal_claim(
        self, run_id: str, goal_id: str, session_id: str
    ) -> bool:
        return bool(
            await self._run_sync(
                self._sync_recover_goal_claim, run_id, goal_id, session_id
            )
        )

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
            session_id = session.id if session else None
            lineage_id: str | None = None
            if session_id is not None:
                attempt_row = c.execute(
                    "SELECT lineage_id FROM attempts WHERE session_id=? "
                    "ORDER BY ordinal DESC LIMIT 1",
                    (session_id,),
                ).fetchone()
                if attempt_row is not None:
                    lineage_id = str(attempt_row["lineage_id"])
            identity = lineage_id or session_id or "run-final"
            goal_id = session.target_goal_id if session else None
            decision_payload = json.dumps(
                {
                    "decision": decision.decision.value,
                    "reason": decision.reason,
                    "score": decision.score,
                    "details": decision.details,
                },
                sort_keys=True,
                default=str,
                separators=(",", ":"),
            )
            decision_digest = hashlib.sha256(
                decision_payload.encode()
            ).hexdigest()[:16]
            idempotency_key = (
                f"{identity}:{goal_id or '-'}:{decision.validator_name}:"
                f"{decision_digest}"
            )
            c.execute(
                """INSERT OR IGNORE INTO validations
                   (id, run_id, session_id, validator, decision, reason, score,
                    details, started_at, duration_ms, idempotency_key)
                   VALUES (?,?,?,?,?,?,?,?,datetime('now'),?,?)""",
                (
                    str(uuid4()),
                    run.id,
                    session_id,
                    decision.validator_name,
                    decision.decision.value,
                    decision.reason,
                    decision.score,
                    json.dumps(decision.details, default=str),
                    decision.duration_ms,
                    idempotency_key,
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

    def _sync_list_nonterminal_runs(self) -> list[Run]:
        terminal = tuple(status.value for status in TERMINAL_RUN_STATUSES)
        placeholders = ",".join("?" for _ in terminal)
        with self._conn() as c:
            rows = c.execute(
                f"SELECT id FROM runs WHERE status NOT IN ({placeholders}) "
                "ORDER BY started_at",
                terminal,
            ).fetchall()
        return [self._sync_load_run(row["id"]) for row in rows]

    async def list_nonterminal_runs(self) -> list[Run]:
        return await self._run_sync(self._sync_list_nonterminal_runs)

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
                    housekeeping_steps=row["housekeeping_steps"],
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
                content={k: v for k, v in json.loads(row["content"]).items() if k != "__horizonx_canonical"},
                canonical=json.loads(row["content"]).get("__horizonx_canonical"),
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
        actor: str,
        instruction: str,
    ) -> tuple[str, Any]:
        from uuid import uuid4

        with self._conn() as c:
            return self._insert_hitl_request(
                c, run_id, trigger, context, hitl_id or str(uuid4()), actor,
                instruction,
            )

    def _insert_hitl_request(
        self, c: sqlite3.Connection, run_id: str, trigger: str,
        context: dict[str, Any], event_id: str, actor: str, instruction: str,
    ) -> tuple[str, Any]:
        from horizonx.core.event_bus import Event

        c.execute(
            "INSERT OR IGNORE INTO hitl_events "
            "(id, run_id, triggered_at, trigger, context, request_actor, "
            "request_reason, request_instruction) VALUES (?,?,?,?,?,?,?,?)",
            (event_id, run_id, utcnow().isoformat(), trigger,
             json.dumps(context, default=str), actor, trigger, instruction),
        )
        request = c.execute(
            "SELECT * FROM hitl_events WHERE id=?", (event_id,)
        ).fetchone()
        if request is None:  # pragma: no cover
            raise StoreError(f"HITL request disappeared after insert: {event_id}")
        requested_id = f"hitl.requested:{event_id}"
        c.execute(
            "INSERT OR IGNORE INTO events "
            "(id, type, run_id, timestamp, payload) VALUES (?,?,?,?,?)",
            (requested_id, "hitl.requested", request["run_id"],
             request["triggered_at"], json.dumps({
                 "request_id": event_id, "reason": request["trigger"],
                 "context": json.loads(request["context"] or "{}"),
                 "actor": request["request_actor"],
                 "instruction": request["request_instruction"] or "",
             }, default=str)),
        )
        event_row = c.execute(
            "SELECT * FROM events WHERE id=?", (requested_id,)
        ).fetchone()
        assert event_row is not None
        return event_id, Event(
            id=event_row["id"], sequence=event_row["sequence"],
            type=event_row["type"], run_id=event_row["run_id"],
            attempt_id=event_row["attempt_id"], session_id=event_row["session_id"],
            goal_id=event_row["goal_id"], timestamp=event_row["timestamp"],
            payload=json.loads(event_row["payload"] or "{}"),
        )

    def _sync_enter_hitl(
        self, run_id: str, trigger: str, context: dict[str, Any],
        hitl_id: str | None, actor: str, instruction: str,
    ) -> tuple[str, Any]:
        from uuid import uuid4

        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            run = c.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(f"run not found: {run_id}")
            if run["status"] != RunStatus.RUNNING.value:
                raise HITLTransitionError(run_id, run["status"])
            changed = c.execute(
                "UPDATE runs SET status=? WHERE id=? AND status=?",
                (RunStatus.PAUSED_HITL.value, run_id, RunStatus.RUNNING.value),
            )
            if changed.rowcount != 1:  # pragma: no cover - write lock fences races
                current = c.execute(
                    "SELECT status FROM runs WHERE id=?", (run_id,)
                ).fetchone()
                raise HITLTransitionError(run_id, current["status"])
            return self._insert_hitl_request(
                c, run_id, trigger, context, hitl_id or str(uuid4()), actor,
                instruction,
            )

    async def enter_hitl(
        self, run_id: str, trigger: str, context: dict[str, Any],
        hitl_id: str | None = None, actor: str = "system", instruction: str = "",
    ) -> tuple[str, Any]:
        """Fence pause transition, audit insertion, and event insertion together."""
        return await self._run_sync(
            self._sync_enter_hitl, run_id, trigger, context, hitl_id, actor,
            instruction,
        )

    async def save_hitl_event(
        self,
        run_id: str,
        trigger: str,
        context: dict[str, Any],
        hitl_id: str | None = None,
        actor: str = "system",
        instruction: str = "",
    ) -> str:
        event_id, _ = await self._run_sync(
            self._sync_save_hitl_event,
            run_id,
            trigger,
            context,
            hitl_id,
            actor,
            instruction,
        )
        return event_id

    async def save_hitl_event_and_event(
        self, run_id: str, trigger: str, context: dict[str, Any],
        hitl_id: str | None = None, actor: str = "system", instruction: str = "",
    ) -> tuple[str, Any]:
        """Persist a HITL request and its stable ledger event atomically."""
        return await self._run_sync(
            self._sync_save_hitl_event, run_id, trigger, context, hitl_id,
            actor, instruction,
        )

    def _sync_ensure_hitl_requested_event(self, request_id: str) -> Any:
        with self._conn() as c:
            request = c.execute(
                "SELECT * FROM hitl_events WHERE id=?", (request_id,)
            ).fetchone()
            if request is None:
                raise KeyError(f"HITL request not found: {request_id}")
        _, event = self._sync_save_hitl_event(
            request["run_id"], request["trigger"],
            json.loads(request["context"] or "{}"), request_id,
            request["request_actor"], request["request_instruction"] or "",
        )
        return event

    async def ensure_hitl_requested_event(self, request_id: str) -> Any:
        """Idempotently backfill the stable request event for legacy rows."""
        return await self._run_sync(self._sync_ensure_hitl_requested_event, request_id)

    def _sync_update_hitl_event(
        self,
        event_id: str,
        action: str,
        operator: str | None,
        instruction: str,
    ) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE hitl_events SET resolved_at=?, decision=?, operator=?, "
                "reason='legacy resolution', instruction=? "
                "WHERE id=? AND resolved_at IS NULL",
                (
                    utcnow().isoformat(),
                    action,
                    operator or "unknown-operator",
                    instruction,
                    event_id,
                ),
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

    def _sync_find_hitl_event(self, event_id: str) -> dict[str, Any]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM hitl_events WHERE id=?", (event_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"HITL request not found: {event_id}")
        return dict(row)

    async def find_hitl_event(self, event_id: str) -> dict[str, Any]:
        return await self._run_sync(self._sync_find_hitl_event, event_id)

    def _sync_resolve_hitl_event(
        self,
        event_id: str,
        action: str,
        actor: str,
        reason: str,
        instruction: str,
        idempotency_key: str,
    ) -> tuple[dict[str, Any], bool, Any]:
        from horizonx.core.event_bus import Event

        now = utcnow().isoformat()
        with self._conn() as c:
            cursor = c.execute(
                "UPDATE hitl_events SET resolved_at=?, decision=?, operator=?, reason=?, "
                "instruction=?, resolution_idempotency_key=? "
                "WHERE id=? AND resolved_at IS NULL",
                (
                    now, action, actor, reason, instruction,
                    idempotency_key, event_id,
                ),
            )
            row = c.execute("SELECT * FROM hitl_events WHERE id=?", (event_id,)).fetchone()
            if row is None:
                raise KeyError(f"HITL request not found: {event_id}")
            event_id_value = f"hitl-resolved:{event_id}"
            c.execute(
                "INSERT OR IGNORE INTO events "
                "(id, type, run_id, timestamp, payload) VALUES (?,?,?,?,?)",
                (
                    event_id_value,
                    "hitl.resolved",
                    row["run_id"],
                    row["resolved_at"] or now,
                    json.dumps(
                        {
                            "request_id": event_id,
                            "action": row["decision"],
                            "actor": row["operator"],
                            "instruction": row["instruction"] or "",
                        }
                    ),
                ),
            )
            event_row = c.execute(
                "SELECT * FROM events WHERE id=?", (event_id_value,)
            ).fetchone()
        assert event_row is not None
        event = Event(
            id=event_row["id"], sequence=event_row["sequence"],
            type=event_row["type"], run_id=event_row["run_id"],
            attempt_id=event_row["attempt_id"], session_id=event_row["session_id"],
            goal_id=event_row["goal_id"], timestamp=event_row["timestamp"],
            payload=json.loads(event_row["payload"] or "{}"),
        )
        return dict(row), cursor.rowcount == 1, event

    async def resolve_hitl_event(
        self,
        event_id: str,
        *,
        action: str,
        actor: str,
        reason: str,
        instruction: str,
        idempotency_key: str,
    ) -> tuple[dict[str, Any], bool]:
        resolved, changed, _ = await self._run_sync(
            self._sync_resolve_hitl_event,
            event_id,
            action,
            actor,
            reason,
            instruction,
            idempotency_key,
        )
        return resolved, changed

    async def resolve_hitl_event_and_event(
        self, event_id: str, *, action: str, actor: str, reason: str,
        instruction: str, idempotency_key: str,
    ) -> tuple[dict[str, Any], bool, Any]:
        """Resolve a HITL request and persist its ledger event in one transaction."""
        return await self._run_sync(
            self._sync_resolve_hitl_event, event_id, action, actor, reason,
            instruction, idempotency_key,
        )

    @staticmethod
    def _operator_command_from_row(row: sqlite3.Row) -> Any:
        from horizonx.core.operator_commands import OperatorCommand

        return OperatorCommand(
            id=row["id"],
            run_id=row["run_id"],
            attempt_id=row["attempt_id"],
            kind=row["kind"],
            actor=row["actor"],
            reason=row["reason"],
            instruction=row["instruction"],
            payload=json.loads(row["payload"] or "{}"),
            idempotency_key=row["idempotency_key"],
            created_at=row["created_at"],
            consumed_at=row["consumed_at"],
        )

    def _sync_create_operator_command(self, command: Any) -> tuple[Any, bool]:
        with self._conn() as c:
            cursor = c.execute(
                "INSERT OR IGNORE INTO operator_commands "
                "(id, run_id, attempt_id, kind, actor, reason, instruction, payload, "
                "idempotency_key, created_at, consumed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    command.id, command.run_id, command.attempt_id,
                    command.kind.value, command.actor, command.reason,
                    command.instruction, json.dumps(command.payload, default=str),
                    command.idempotency_key, command.created_at.isoformat(),
                    command.consumed_at.isoformat() if command.consumed_at else None,
                ),
            )
            row = c.execute(
                "SELECT * FROM operator_commands WHERE run_id=? AND idempotency_key=?",
                (command.run_id, command.idempotency_key),
            ).fetchone()
        if row is None:  # pragma: no cover
            raise StoreError("operator command disappeared after insert")
        if cursor.rowcount == 0 and (
            row["kind"] != command.kind.value
            or row["actor"] != command.actor
            or row["reason"] != command.reason
            or row["instruction"] != command.instruction
            or json.loads(row["payload"] or "{}") != command.payload
        ):
            raise OperatorCommandConflict(
                "operator command idempotency key was reused with different content"
            )
        return self._operator_command_from_row(row), cursor.rowcount == 1

    async def create_operator_command(self, command: Any) -> tuple[Any, bool]:
        return await self._run_sync(self._sync_create_operator_command, command)

    def _sync_submit_cancel_command(self, candidate: Any) -> tuple[Any, bool, dict[str, Any]]:
        """Accept and apply cancellation atomically, fencing the active lease."""
        now = utcnow().isoformat()
        with self._conn() as c:
            existing = c.execute(
                "SELECT * FROM operator_commands WHERE run_id=? AND idempotency_key=?",
                (candidate.run_id, candidate.idempotency_key),
            ).fetchone()
            if existing is not None:
                command = self._operator_command_from_row(existing)
                if (
                    existing["kind"] != candidate.kind.value
                    or existing["actor"] != candidate.actor
                    or existing["reason"] != candidate.reason
                    or existing["instruction"] != candidate.instruction
                    or json.loads(existing["payload"] or "{}") != candidate.payload
                ):
                    raise OperatorCommandConflict(
                        "operator command idempotency key was reused with different content"
                    )
                return command, False, {"run_id": candidate.run_id, "event": None}

            run = c.execute("SELECT status FROM runs WHERE id=?", (candidate.run_id,)).fetchone()
            if run is None:
                raise KeyError(f"run not found: {candidate.run_id}")
            if run["status"] in {status.value for status in TERMINAL_RUN_STATUSES}:
                raise StoreError(f"run is already terminal: {run['status']}")
            c.execute(
                "INSERT INTO operator_commands "
                "(id, run_id, attempt_id, kind, actor, reason, instruction, payload, "
                "idempotency_key, created_at, consumed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (candidate.id, candidate.run_id, candidate.attempt_id, candidate.kind.value,
                 candidate.actor, candidate.reason, candidate.instruction,
                 json.dumps(candidate.payload, default=str), candidate.idempotency_key,
                 candidate.created_at.isoformat(), now),
            )
            result = self._apply_cancel_in_transaction(c, candidate.id, now)
            row = c.execute("SELECT * FROM operator_commands WHERE id=?", (candidate.id,)).fetchone()
        assert row is not None
        return self._operator_command_from_row(row), True, result

    async def submit_cancel_command(self, candidate: Any) -> tuple[Any, bool, dict[str, Any]]:
        return await self._run_sync(self._sync_submit_cancel_command, candidate)

    def _sync_get_operator_command(
        self, run_id: str, idempotency_key: str
    ) -> Any | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM operator_commands WHERE run_id=? AND idempotency_key=?",
                (run_id, idempotency_key),
            ).fetchone()
        return self._operator_command_from_row(row) if row is not None else None

    async def get_operator_command(
        self, run_id: str, idempotency_key: str
    ) -> Any | None:
        return await self._run_sync(
            self._sync_get_operator_command, run_id, idempotency_key
        )

    def _sync_list_operator_commands(
        self, run_id: str, unconsumed_only: bool
    ) -> list[Any]:
        query = "SELECT * FROM operator_commands WHERE run_id=?"
        if unconsumed_only:
            query += " AND consumed_at IS NULL"
        query += " ORDER BY created_at, id"
        with self._conn() as c:
            rows = c.execute(query, (run_id,)).fetchall()
        return [self._operator_command_from_row(row) for row in rows]

    async def list_operator_commands(
        self, run_id: str, *, unconsumed_only: bool = False
    ) -> list[Any]:
        return await self._run_sync(
            self._sync_list_operator_commands, run_id, unconsumed_only
        )

    def _sync_consume_operator_command(
        self, command_id: str, attempt_id: str | None
    ) -> Any:
        with self._conn() as c:
            c.execute(
                "UPDATE operator_commands SET consumed_at=COALESCE(consumed_at, ?), "
                "attempt_id=COALESCE(attempt_id, ?) WHERE id=?",
                (utcnow().isoformat(), attempt_id, command_id),
            )
            row = c.execute(
                "SELECT * FROM operator_commands WHERE id=?", (command_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"operator command not found: {command_id}")
        return self._operator_command_from_row(row)

    async def consume_operator_command(
        self, command_id: str, *, attempt_id: str | None = None
    ) -> Any:
        return await self._run_sync(
            self._sync_consume_operator_command, command_id, attempt_id
        )

    def _apply_cancel_in_transaction(
        self, c: sqlite3.Connection, command_id: str, now: str
    ) -> dict[str, Any]:
        command = c.execute(
            "SELECT * FROM operator_commands WHERE id=?", (command_id,)
        ).fetchone()
        if command is None:
            raise KeyError(f"operator command not found: {command_id}")
        if command["kind"] != "cancel":
            raise StoreError("operator command is not a cancellation")
        request = c.execute(
            "SELECT * FROM hitl_events WHERE run_id=? AND resolved_at IS NULL "
            "ORDER BY triggered_at DESC LIMIT 1", (command["run_id"],),
        ).fetchone()
        if request is not None:
            c.execute(
                "UPDATE hitl_events SET resolved_at=?, decision='abort', operator=?, "
                "reason=?, instruction=?, resolution_idempotency_key=? WHERE id=?",
                (now, command["actor"], command["reason"], command["instruction"],
                 command["idempotency_key"], request["id"]),
            )
            event_id = f"hitl-resolved:{request['id']}"
            c.execute(
                "INSERT OR IGNORE INTO events "
                "(id, type, run_id, timestamp, payload) VALUES (?,?,?,?,?)",
                (event_id, "hitl.resolved", command["run_id"], now,
                 json.dumps({"request_id": request["id"], "action": "abort",
                             "actor": command["actor"],
                             "instruction": command["instruction"] or ""})),
            )
        attempt = c.execute(
            "SELECT id FROM attempts WHERE run_id=? ORDER BY ordinal DESC LIMIT 1",
            (command["run_id"],),
        ).fetchone()
        if attempt is not None:
            c.execute(
                "UPDATE attempts SET status='aborted', error=?, updated_at=?, "
                "completed_at=COALESCE(completed_at, ?), version=version+1 "
                "WHERE id=? AND status NOT IN ('completed','failed','interrupted','aborted')",
                (f"operator_cancel:{command['reason'] or command['actor']}", now, now,
                 attempt["id"]),
            )
        c.execute(
            "UPDATE runs SET status='aborted', completed_at=COALESCE(completed_at, ?) "
            "WHERE id=? AND status NOT IN "
            "('completed','failed','aborted','timed_out','budget_exceeded')",
            (now, command["run_id"]),
        )
        c.execute(
            "UPDATE operator_commands SET consumed_at=COALESCE(consumed_at, ?), "
            "attempt_id=COALESCE(attempt_id, ?) WHERE id=?",
            (now, attempt["id"] if attempt else None, command_id),
        )
        c.execute(
            "UPDATE leases SET owner='', expires_at='1970-01-01T00:00:00+00:00', "
            "version=version+1 WHERE resource_id=?", (f"run:{command['run_id']}",),
        )
        return {
            "run_id": command["run_id"],
            "request_id": request["id"] if request else None,
            "attempt_id": attempt["id"] if attempt else None,
            "actor": command["actor"],
            "reason": command["reason"],
            "instruction": command["instruction"],
            "event_id": f"hitl-resolved:{request['id']}" if request else None,
        }

    def _sync_apply_cancel_command(self, command_id: str) -> dict[str, Any]:
        now = utcnow().isoformat()
        with self._conn() as c:
            return self._apply_cancel_in_transaction(c, command_id, now)

    async def apply_cancel_command(self, command_id: str) -> dict[str, Any]:
        """Atomically resolve pending HITL, abort work, and consume cancellation."""
        return await self._run_sync(self._sync_apply_cancel_command, command_id)

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
    # Append-only events
    # ------------------------------------------------------------------

    def _sync_append_event(self, event: Any) -> Any:
        from horizonx.core.event_bus import Event

        attempt_id = event.attempt_id
        goal_id = event.goal_id
        if attempt_id is None and event.session_id is not None:
            with self._conn() as c:
                attempt_row = c.execute(
                    "SELECT id, goal_id FROM attempts WHERE session_id=? "
                    "ORDER BY ordinal DESC LIMIT 1",
                    (event.session_id,),
                ).fetchone()
            if attempt_row is not None:
                attempt_id = attempt_row["id"]
                goal_id = goal_id or attempt_row["goal_id"]
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO events "
                "(id, type, run_id, attempt_id, session_id, goal_id, timestamp, payload) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    event.id,
                    event.type,
                    event.run_id,
                    attempt_id,
                    event.session_id,
                    goal_id,
                    event.timestamp.isoformat(),
                    json.dumps(event.payload, default=str),
                ),
            )
            row = c.execute("SELECT * FROM events WHERE id=?", (event.id,)).fetchone()
            if row is None and event.type == "strategy.switched":
                row = c.execute(
                    "SELECT * FROM events WHERE run_id=? AND type='strategy.switched'",
                    (event.run_id,),
                ).fetchone()
        if row is None:  # pragma: no cover - insert or existing row must be visible
            raise StoreError(f"event disappeared after append: {event.id}")
        return Event(
            id=row["id"],
            sequence=row["sequence"],
            type=row["type"],
            run_id=row["run_id"],
            attempt_id=row["attempt_id"],
            session_id=row["session_id"],
            goal_id=row["goal_id"],
            timestamp=row["timestamp"],
            payload=json.loads(row["payload"] or "{}"),
        )

    async def append_event(self, event: Any) -> Any:
        return await self._run_sync(self._sync_append_event, event)

    def _sync_list_events(
        self,
        run_id: str,
        after_sequence: int | None,
        limit: int,
        event_type: str | None,
    ) -> list[Any]:
        from horizonx.core.event_bus import Event

        clauses = ["run_id=?"]
        values: list[Any] = [run_id]
        if after_sequence is not None:
            clauses.append("sequence>?")
            values.append(after_sequence)
        if event_type is not None:
            clauses.append("type=?")
            values.append(event_type)
        values.append(limit)
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM events WHERE "
                + " AND ".join(clauses)
                + " ORDER BY sequence LIMIT ?",
                values,
            ).fetchall()
        return [
            Event(
                id=row["id"],
                sequence=row["sequence"],
                type=row["type"],
                run_id=row["run_id"],
                attempt_id=row["attempt_id"],
                session_id=row["session_id"],
                goal_id=row["goal_id"],
                timestamp=row["timestamp"],
                payload=json.loads(row["payload"] or "{}"),
            )
            for row in rows
        ]

    async def list_events(
        self,
        run_id: str,
        *,
        after_sequence: int | None = None,
        limit: int = 1000,
        event_type: str | None = None,
    ) -> list[Any]:
        return await self._run_sync(
            self._sync_list_events,
            run_id,
            after_sequence,
            limit,
            event_type,
        )

    def _sync_list_all_events(
        self, after_sequence: int | None, limit: int
    ) -> list[Any]:
        from horizonx.core.event_bus import Event

        query = "SELECT * FROM events"
        values: list[Any] = []
        if after_sequence is not None:
            query += " WHERE sequence>?"
            values.append(after_sequence)
        query += " ORDER BY sequence LIMIT ?"
        values.append(limit)
        with self._conn() as c:
            rows = c.execute(query, values).fetchall()
        return [
            Event(
                id=row["id"], sequence=row["sequence"], type=row["type"],
                run_id=row["run_id"], attempt_id=row["attempt_id"],
                session_id=row["session_id"], goal_id=row["goal_id"],
                timestamp=row["timestamp"], payload=json.loads(row["payload"] or "{}"),
            )
            for row in rows
        ]

    async def list_all_events(
        self, *, after_sequence: int | None = None, limit: int = 1000
    ) -> list[Any]:
        return await self._run_sync(
            self._sync_list_all_events, after_sequence, limit
        )

    # ------------------------------------------------------------------
    # Versioned expiring leases
    # ------------------------------------------------------------------

    @staticmethod
    def _lease_from_row(row: sqlite3.Row) -> LeaseRecord:
        return LeaseRecord(
            resource_id=row["resource_id"],
            owner=row["owner"],
            acquired_at=row["acquired_at"],
            heartbeat_at=row["heartbeat_at"],
            expires_at=row["expires_at"],
            version=row["version"],
        )

    def _sync_acquire_lease(
        self,
        resource_id: str,
        owner: str,
        ttl_seconds: float,
        now: datetime,
    ) -> LeaseRecord | None:
        conn = sqlite3.connect(self.db_path, timeout=self.busy_timeout_ms / 1000)
        conn.row_factory = sqlite3.Row
        _configure_connection(conn, self.busy_timeout_ms)
        expires_at = now + timedelta(seconds=ttl_seconds)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM leases WHERE resource_id=?", (resource_id,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO leases "
                    "(resource_id, owner, acquired_at, heartbeat_at, expires_at, version) "
                    "VALUES (?,?,?,?,?,1)",
                    (
                        resource_id,
                        owner,
                        now.isoformat(),
                        now.isoformat(),
                        expires_at.isoformat(),
                    ),
                )
            elif datetime.fromisoformat(row["expires_at"]) <= now:
                cur = conn.execute(
                    "UPDATE leases SET owner=?, acquired_at=?, heartbeat_at=?, "
                    "expires_at=?, version=version+1 WHERE resource_id=? AND version=?",
                    (
                        owner,
                        now.isoformat(),
                        now.isoformat(),
                        expires_at.isoformat(),
                        resource_id,
                        row["version"],
                    ),
                )
                if cur.rowcount != 1:
                    conn.rollback()
                    return None
            else:
                conn.rollback()
                return None
            conn.commit()
            claimed = conn.execute(
                "SELECT * FROM leases WHERE resource_id=?", (resource_id,)
            ).fetchone()
            return self._lease_from_row(claimed)
        finally:
            conn.close()

    async def acquire_lease(
        self,
        resource_id: str,
        owner: str,
        ttl_seconds: float,
        now: datetime,
    ) -> LeaseRecord | None:
        return await self._run_sync(
            self._sync_acquire_lease, resource_id, owner, ttl_seconds, now
        )

    def _sync_get_lease(self, resource_id: str) -> LeaseRecord | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM leases WHERE resource_id=? AND owner<>''",
                (resource_id,),
            ).fetchone()
        return self._lease_from_row(row) if row else None

    async def get_lease(self, resource_id: str) -> LeaseRecord | None:
        return await self._run_sync(self._sync_get_lease, resource_id)

    def _sync_heartbeat_lease(
        self,
        resource_id: str,
        owner: str,
        version: int,
        ttl_seconds: float,
        now: datetime,
    ) -> LeaseRecord | None:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE leases SET heartbeat_at=?, expires_at=? "
                "WHERE resource_id=? AND owner=? AND version=? AND expires_at>?",
                (
                    now.isoformat(),
                    (now + timedelta(seconds=ttl_seconds)).isoformat(),
                    resource_id,
                    owner,
                    version,
                    now.isoformat(),
                ),
            )
            if cur.rowcount != 1:
                return None
        return self._sync_get_lease(resource_id)

    async def heartbeat_lease(
        self,
        resource_id: str,
        owner: str,
        version: int,
        ttl_seconds: float,
        now: datetime,
    ) -> LeaseRecord | None:
        return await self._run_sync(
            self._sync_heartbeat_lease,
            resource_id,
            owner,
            version,
            ttl_seconds,
            now,
        )

    def _sync_release_lease(
        self, resource_id: str, owner: str, version: int
    ) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE leases SET owner='', expires_at='1970-01-01T00:00:00+00:00' "
                "WHERE resource_id=? AND owner=? AND version=?",
                (resource_id, owner, version),
            )
        return int(cur.rowcount) == 1

    async def release_lease(
        self, resource_id: str, owner: str, version: int
    ) -> bool:
        return bool(
            await self._run_sync(
                self._sync_release_lease, resource_id, owner, version
            )
        )

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

    def _sync_list_pending_runs(self, include_started: bool) -> list[dict[str, Any]]:
        with self._conn() as c:
            if include_started:
                rows = c.execute(
                    "SELECT run_id, task_json FROM pending_runs "
                    "WHERE status IN ('pending','started')"
                ).fetchall()
            else:
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

    async def list_pending_runs(
        self, *, include_started: bool = False
    ) -> list[dict[str, Any]]:
        return await self._run_sync(self._sync_list_pending_runs, include_started)

    # ------------------------------------------------------------------
    # Workspace usage (cross-run daily budget tracking)
    # ------------------------------------------------------------------

    def _sync_record_workspace_usage(
        self, workspace_id: str, run_id: str,
        tokens_in: int, tokens_out: int, usd: float | None,
    ) -> None:
        import uuid
        from datetime import date
        with self._conn() as c:
            c.execute(
                "INSERT INTO workspace_usage "
                "(id, workspace_id, run_id, date, tokens_in, tokens_out, usd, usd_known, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
                (str(uuid.uuid4()), workspace_id, run_id, str(date.today()),
                 tokens_in, tokens_out, usd or 0.0, int(usd is not None)),
            )

    def _sync_workspace_daily_usd(self, workspace_id: str) -> float | None:
        from datetime import date
        with self._conn() as c:
            row = c.execute(
                "SELECT COALESCE(SUM(usd), 0.0), COALESCE(MIN(usd_known), 1) "
                "FROM workspace_usage "
                "WHERE workspace_id=? AND date=?",
                (workspace_id, str(date.today())),
            ).fetchone()
        if row and not bool(row[1]):
            return None
        return float(row[0]) if row else 0.0

    async def record_workspace_usage(
        self, workspace_id: str, run_id: str,
        tokens_in: int, tokens_out: int, usd: float | None,
    ) -> None:
        return await self._run_sync(
            self._sync_record_workspace_usage,
            workspace_id, run_id, tokens_in, tokens_out, usd,
        )

    async def workspace_daily_usd(self, workspace_id: str) -> float | None:
        return await self._run_sync(self._sync_workspace_daily_usd, workspace_id)
