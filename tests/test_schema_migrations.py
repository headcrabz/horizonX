"""Schema migration tests use real legacy SQLite files, not mocked connections."""

from __future__ import annotations

import asyncio
import importlib
import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from horizonx.core.types import GoalNode
from horizonx.storage.migrations import SchemaMigrationError
from horizonx.storage.sqlite import SqliteStore, StoreBusyError

LEGACY_GOALS_SCHEMA = """
CREATE TABLE goals (
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
"""


def _write_v01_database(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(LEGACY_GOALS_SCHEMA)
        conn.executemany(
            """
            INSERT INTO goals (
                id, run_id, parent_id, name, description, verification_criteria,
                status, attempts, notes, last_updated_at, last_updated_by_session,
                assigned_to_session, validators, inherit_validators
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "g.root",
                    "legacy-run",
                    None,
                    "Legacy root",
                    "root description",
                    "[]",
                    "in_progress",
                    1,
                    "root note",
                    "2026-01-01T00:00:00+00:00",
                    "sess-legacy",
                    "sess-legacy",
                    "[]",
                    1,
                ),
                (
                    "g.child",
                    "legacy-run",
                    "g.root",
                    "Legacy child",
                    "child description",
                    '["legacy criterion"]',
                    "pending",
                    2,
                    "child note",
                    "2026-01-01T00:01:00+00:00",
                    None,
                    None,
                    "[]",
                    0,
                ),
            ],
        )


@pytest.mark.asyncio
async def test_v01_goal_schema_migrates_without_losing_stored_fields(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    _write_v01_database(path)

    store = SqliteStore(path)
    try:
        assert await store.schema_version() == 4

        root = await store.load_goal("legacy-run", "g.root")
        child = await store.load_goal("legacy-run", "g.child")
        assert root is not None
        assert child is not None
        assert root.name == "Legacy root"
        assert root.status.value == "in_progress"
        assert root.attempts == 1
        assert root.notes == "root note"
        assert root.assigned_to_session == "sess-legacy"
        assert root.children == ["g.child"]
        assert child.parent_id == "g.root"
        assert child.verification_criteria == ["legacy criterion"]
        assert child.inherit_validators is False

        await store.save_goal(
            "second-run", GoalNode(id="g.root", name="Second root", description="second")
        )
        assert (await store.load_goal("second-run", "g.root")) is not None
    finally:
        await store.close()

    with sqlite3.connect(path) as conn:
        pk_columns = [
            row[1]
            for row in sorted(
                (row for row in conn.execute("PRAGMA table_info(goals)") if row[5]),
                key=lambda row: row[5],
            )
        ]
        assert pk_columns == ["run_id", "id"]
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.asyncio
async def test_legacy_zero_cost_usage_migrates_as_unknown(tmp_path: Path) -> None:
    path = tmp_path / "legacy-usage.db"
    initial = SqliteStore(path)
    await initial.close()
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE workspace_usage RENAME TO workspace_usage_v3")
        conn.execute(
            """
            CREATE TABLE workspace_usage (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                date TEXT NOT NULL,
                tokens_in INTEGER NOT NULL DEFAULT 0,
                tokens_out INTEGER NOT NULL DEFAULT 0,
                usd REAL NOT NULL DEFAULT 0.0,
                recorded_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO workspace_usage VALUES "
            "('usage-legacy', 'workspace-legacy', 'run-codex', date('now'), "
            "100, 50, 0.0, datetime('now'))"
        )
        conn.execute("DROP TABLE workspace_usage_v3")

    migrated = SqliteStore(path)
    try:
        assert await migrated.workspace_daily_usd("workspace-legacy") is None
    finally:
        await migrated.close()


def test_malformed_legacy_schema_fails_explicitly(tmp_path: Path) -> None:
    path = tmp_path / "malformed.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE goals (id TEXT PRIMARY KEY, run_id TEXT NOT NULL)")

    with pytest.raises(SchemaMigrationError, match="missing required columns"):
        SqliteStore(path)


def test_newer_schema_version_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    store = SqliteStore(path)

    async def close_store() -> None:
        await store.close()

    asyncio.run(close_store())
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO schema_migrations(version) VALUES (999)")

    with pytest.raises(SchemaMigrationError, match="newer schema version"):
        SqliteStore(path)


def test_incomplete_composite_schema_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "incomplete.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE goals (
                run_id TEXT NOT NULL,
                id TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                verification_criteria TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL,
                last_updated_at TEXT NOT NULL,
                PRIMARY KEY (run_id, id)
            )
            """
        )

    with pytest.raises(SchemaMigrationError, match="missing columns"):
        SqliteStore(path)


@pytest.mark.asyncio
async def test_rejected_restore_preserves_live_database(tmp_path: Path) -> None:
    live_path = tmp_path / "live.db"
    future_path = tmp_path / "future.db"
    live = SqliteStore(live_path)
    future = SqliteStore(future_path)
    try:
        await live.save_goal(
            "live-run",
            GoalNode(id="g.root", name="Keep me", description="live state"),
        )
        await future.save_goal(
            "future-run",
            GoalNode(id="g.root", name="Reject me", description="future state"),
        )
    finally:
        await future.close()

    with sqlite3.connect(future_path) as conn:
        conn.execute("INSERT INTO schema_migrations(version) VALUES (999)")

    try:
        with pytest.raises(SchemaMigrationError, match="newer schema version"):
            await live.restore(future_path)

        preserved = await live.load_goal("live-run", "g.root")
        assert preserved is not None
        assert preserved.name == "Keep me"
        assert await live.load_goal("future-run", "g.root") is None
    finally:
        await live.close()


@pytest.mark.asyncio
async def test_restore_rejects_a_busy_live_database_without_replacing_it(
    tmp_path: Path,
) -> None:
    live_path = tmp_path / "live.db"
    source_path = tmp_path / "source.db"
    live = SqliteStore(live_path, busy_timeout_ms=25)
    source = SqliteStore(source_path)
    try:
        await live.save_goal(
            "live-run", GoalNode(id="g.root", name="Live", description="keep")
        )
        await source.save_goal(
            "source-run", GoalNode(id="g.root", name="Source", description="new")
        )
    finally:
        await source.close()

    blocker = sqlite3.connect(live_path)
    blocker.execute("BEGIN")
    blocker.execute("SELECT * FROM goals").fetchall()
    try:
        with pytest.raises(StoreBusyError, match="restore requires exclusive"):
            await live.restore(source_path)
    finally:
        blocker.close()

    try:
        preserved = await live.load_goal("live-run", "g.root")
        assert preserved is not None
        assert preserved.name == "Live"
        assert await live.load_goal("source-run", "g.root") is None
    finally:
        await live.close()


@pytest.mark.asyncio
async def test_restore_syncs_database_and_directory_before_success(
    tmp_path: Path,
) -> None:
    live_path = tmp_path / "live.db"
    source_path = tmp_path / "source.db"
    live = SqliteStore(live_path)
    source = SqliteStore(source_path)
    await source.close()
    try:
        with patch("horizonx.storage.sqlite.os.fsync") as fsync:
            await live.restore(source_path)

        assert fsync.call_count == 2
    finally:
        await live.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX lock assertion")
@pytest.mark.asyncio
async def test_restore_holds_maintenance_lock_through_atomic_replace(
    tmp_path: Path,
) -> None:
    live_path = tmp_path / "live.db"
    source_path = tmp_path / "source.db"
    live = SqliteStore(live_path)
    source = SqliteStore(source_path)
    await source.close()
    real_replace = os.replace
    fcntl = importlib.import_module("fcntl")

    def replace_while_locked(source: str | Path, destination: str | Path) -> None:
        lock_fd = os.open(f"{live_path}.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(lock_fd)
        real_replace(source, destination)

    try:
        with patch(
            "horizonx.storage.sqlite.os.replace", side_effect=replace_while_locked
        ):
            await live.restore(source_path)
    finally:
        await live.close()
