"""Schema migration tests use real legacy SQLite files, not mocked connections."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from horizonx.core.types import GoalNode
from horizonx.storage.migrations import SchemaMigrationError
from horizonx.storage.sqlite import SqliteStore

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
        assert await store.schema_version() == 3

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
