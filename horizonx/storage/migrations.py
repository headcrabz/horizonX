"""Explicit SQLite schema migrations for HorizonX."""

from __future__ import annotations

import sqlite3

CURRENT_SCHEMA_VERSION = 5

_REQUIRED_GOAL_COLUMNS = {
    "run_id",
    "id",
    "name",
    "description",
    "verification_criteria",
    "status",
    "attempts",
    "max_attempts",
    "progress_pct",
    "version",
    "notes",
    "last_updated_at",
    "last_updated_by_session",
    "assigned_to_session",
    "validators",
    "inherit_validators",
}
_REQUIRED_GOAL_EDGE_COLUMNS = {
    "run_id",
    "from_goal_id",
    "to_goal_id",
    "edge_type",
    "position",
}
_REQUIRED_DURABILITY_TABLES = {"attempts", "events", "leases", "operator_commands"}


class SchemaMigrationError(RuntimeError):
    """Raised when a database cannot be migrated safely."""


_LATEST_GOALS_SQL = """
CREATE TABLE goals (
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
)
"""

_LATEST_GOAL_EDGES_SQL = """
CREATE TABLE goal_edges (
    run_id       TEXT NOT NULL,
    from_goal_id TEXT NOT NULL,
    to_goal_id   TEXT NOT NULL,
    edge_type    TEXT NOT NULL CHECK (edge_type IN ('parent', 'dependency')),
    position     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, from_goal_id, to_goal_id, edge_type),
    FOREIGN KEY (run_id, from_goal_id) REFERENCES goals(run_id, id) ON DELETE CASCADE,
    FOREIGN KEY (run_id, to_goal_id) REFERENCES goals(run_id, id) ON DELETE CASCADE
)
"""


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _goal_primary_key(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("PRAGMA table_info(goals)").fetchall()
    return [row[1] for row in sorted((row for row in rows if row[5]), key=lambda row: row[5])]


def _legacy_value(columns: set[str], name: str, fallback: str) -> str:
    return name if name in columns else fallback


def prepare_schema(conn: sqlite3.Connection) -> None:
    """Create migration metadata and upgrade legacy goal identity when required."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        latest_row = conn.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()
        latest_version = int(latest_row[0] or 0)
        if latest_version > CURRENT_SCHEMA_VERSION:
            raise SchemaMigrationError(
                "database uses newer schema version "
                f"{latest_version}; this release supports up to {CURRENT_SCHEMA_VERSION}"
            )
        if not _table_exists(conn, "goals") or _goal_primary_key(conn) == ["run_id", "id"]:
            conn.commit()
            return

        columns = {row[1] for row in conn.execute("PRAGMA table_info(goals)")}
        required = {
            "id",
            "run_id",
            "name",
            "description",
            "verification_criteria",
            "status",
            "attempts",
            "last_updated_at",
        }
        missing = required - columns
        if missing:
            raise SchemaMigrationError(
                f"legacy goals table is missing required columns: {sorted(missing)}"
            )

        conn.execute("ALTER TABLE goals RENAME TO goals_v01")
        conn.execute(_LATEST_GOALS_SQL)
        conn.execute(_LATEST_GOAL_EDGES_SQL)
        conn.execute("CREATE INDEX idx_goals_run ON goals(run_id, status)")
        conn.execute(
            "CREATE INDEX idx_goal_edges_to "
            "ON goal_edges(run_id, to_goal_id, edge_type)"
        )

        select_expressions = [
            "run_id",
            "id",
            "name",
            "description",
            "verification_criteria",
            "status",
            "attempts",
            _legacy_value(columns, "max_attempts", "3"),
            _legacy_value(columns, "progress_pct", "0.0"),
            _legacy_value(columns, "version", "0"),
            _legacy_value(columns, "notes", "''"),
            "last_updated_at",
            _legacy_value(columns, "last_updated_by_session", "NULL"),
            _legacy_value(columns, "assigned_to_session", "NULL"),
            _legacy_value(columns, "validators", "'[]'"),
            _legacy_value(columns, "inherit_validators", "1"),
        ]
        conn.execute(
            "INSERT INTO goals (run_id, id, name, description, verification_criteria, status, "
            "attempts, max_attempts, progress_pct, version, notes, last_updated_at, "
            "last_updated_by_session, assigned_to_session, validators, inherit_validators) "
            f"SELECT {', '.join(select_expressions)} FROM goals_v01"
        )
        if "parent_id" in columns:
            conn.execute(
                """
                INSERT INTO goal_edges (
                    run_id, from_goal_id, to_goal_id, edge_type, position
                )
                SELECT child.run_id, child.parent_id, child.id, 'parent', 0
                FROM goals_v01 AS child
                JOIN goals_v01 AS parent
                  ON parent.run_id = child.run_id AND parent.id = child.parent_id
                WHERE child.parent_id IS NOT NULL
                """
            )
        conn.execute("DROP TABLE goals_v01")
        conn.commit()
    except Exception as exc:
        conn.rollback()
        if isinstance(exc, SchemaMigrationError):
            raise
        raise SchemaMigrationError(f"failed to migrate SQLite schema: {exc}") from exc


def ensure_additive_schema(conn: sqlite3.Connection) -> None:
    """Add columns and indexes that SQLite can upgrade without table replacement."""
    if _table_exists(conn, "validations"):
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(validations)")
        }
        if "idempotency_key" not in columns:
            conn.execute("ALTER TABLE validations ADD COLUMN idempotency_key TEXT")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_validations_idempotency "
            "ON validations(idempotency_key) WHERE idempotency_key IS NOT NULL"
        )
    if _table_exists(conn, "workspace_usage"):
        usage_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(workspace_usage)")
        }
        if "usd_known" not in usage_columns:
            conn.execute(
                "ALTER TABLE workspace_usage "
                "ADD COLUMN usd_known INTEGER NOT NULL DEFAULT 0"
            )
            conn.execute(
                "UPDATE workspace_usage SET usd_known=1 WHERE usd != 0.0"
            )
    if _table_exists(conn, "hitl_events"):
        hitl_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(hitl_events)")
        }
        additions = {
            "request_actor": "TEXT NOT NULL DEFAULT 'system'",
            "request_reason": "TEXT NOT NULL DEFAULT ''",
            "request_instruction": "TEXT NOT NULL DEFAULT ''",
            "reason": "TEXT NOT NULL DEFAULT ''",
            "resolution_idempotency_key": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in hitl_columns:
                conn.execute(
                    f"ALTER TABLE hitl_events ADD COLUMN {name} {declaration}"
                )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_hitl_resolution_idempotency "
            "ON hitl_events(resolution_idempotency_key) "
            "WHERE resolution_idempotency_key IS NOT NULL"
        )


def record_current_schema(conn: sqlite3.Connection) -> None:
    goal_columns = {row[1] for row in conn.execute("PRAGMA table_info(goals)")}
    missing_goal_columns = _REQUIRED_GOAL_COLUMNS - goal_columns
    if missing_goal_columns:
        raise SchemaMigrationError(
            f"current goals schema is missing columns: {sorted(missing_goal_columns)}"
        )
    edge_columns = {row[1] for row in conn.execute("PRAGMA table_info(goal_edges)")}
    missing_edge_columns = _REQUIRED_GOAL_EDGE_COLUMNS - edge_columns
    if missing_edge_columns:
        raise SchemaMigrationError(
            f"current goal_edges schema is missing columns: {sorted(missing_edge_columns)}"
        )
    if _goal_primary_key(conn) != ["run_id", "id"]:
        raise SchemaMigrationError("current goals schema does not use run-scoped identity")
    foreign_keys = {
        (row[2], row[3], row[4])
        for row in conn.execute("PRAGMA foreign_key_list(goal_edges)")
    }
    required_foreign_keys = {
        ("goals", "run_id", "run_id"),
        ("goals", "from_goal_id", "id"),
        ("goals", "to_goal_id", "id"),
    }
    if not required_foreign_keys.issubset(foreign_keys):
        raise SchemaMigrationError("current goal_edges schema is missing foreign keys")
    missing_tables = {
        table for table in _REQUIRED_DURABILITY_TABLES if not _table_exists(conn, table)
    }
    if missing_tables:
        raise SchemaMigrationError(
            f"current durability schema is missing tables: {sorted(missing_tables)}"
        )
    validation_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(validations)")
    }
    if "idempotency_key" not in validation_columns:
        raise SchemaMigrationError(
            "current validations schema is missing idempotency_key"
        )
    usage_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(workspace_usage)")
    }
    if "usd_known" not in usage_columns:
        raise SchemaMigrationError(
            "current workspace_usage schema is missing usd_known"
        )
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)",
        (CURRENT_SCHEMA_VERSION,),
    )


def read_schema_version(conn: sqlite3.Connection) -> int:
    if not _table_exists(conn, "schema_migrations"):
        return 0
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0] or 0)
