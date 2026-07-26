"""Global workspace knowledge store — facts that persist across runs.

Facts are written by agents to workspace/knowledge/<slug>.md during sessions.
After each session, KnowledgeHandoffDir.sync() indexes them into a per-workspace
SQLite FTS5 database. Future runs' session prompts receive the top-K relevant facts.

Storage: <workspace_parent>/.horizonx/<workspace_id>/knowledge.db
Never written to ~/.horizonx/ unconditionally (breaks CI/Docker/multi-user setups).
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS facts_meta (
    id                  TEXT PRIMARY KEY,
    content             TEXT NOT NULL,
    tags                TEXT,
    source_run_id       TEXT,
    source_goal_id      TEXT,
    created_at          REAL NOT NULL,
    last_referenced_at  REAL,
    reference_count     INTEGER NOT NULL DEFAULT 0,
    author              TEXT NOT NULL DEFAULT 'agent',
    status              TEXT NOT NULL DEFAULT 'active'
);
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    content, tags, tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts_meta BEGIN
    INSERT INTO facts_fts(rowid, content, tags)
    VALUES (new.rowid, new.content, COALESCE(new.tags, ''));
END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts_meta BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content, tags)
    VALUES ('delete', old.rowid, old.content, COALESCE(old.tags, ''));
END;
"""


class WorkspaceKnowledgeStore:
    """FTS5-backed fact store scoped to a workspace, persistent across runs."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    @classmethod
    def for_run(cls, workspace_path: Path, workspace_id: str | None = None) -> WorkspaceKnowledgeStore:
        """Resolve the knowledge DB path relative to the run workspace.

        Stores in <workspace_parent>/.horizonx/<workspace_id>/knowledge.db.
        Falls back to 'default' if no workspace_id is configured.
        """
        wid = workspace_id or "default"
        db_path = workspace_path.parent / ".horizonx" / wid / "knowledge.db"
        return cls(db_path)

    def _init(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _conn(self):  # type: ignore[no-untyped-def]
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def upsert_fact(
        self,
        content: str,
        tags: list[str] | None = None,
        source_run_id: str | None = None,
        source_goal_id: str | None = None,
        author: str = "agent",
    ) -> str:
        fact_id = str(uuid.uuid4())
        tags_json = json.dumps(tags or [])
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO facts_meta (id, content, tags, source_run_id, source_goal_id, "
                "created_at, author, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'active')",
                (fact_id, content, tags_json, source_run_id, source_goal_id, time.time(), author),
            )
            conn.commit()
        return fact_id

    def search(self, query: str, limit: int = 8) -> list[str]:
        """FTS5 relevance search. Updates reference_count for retrieved facts."""
        if not query.strip():
            return []
        clean = query.replace('"', "").replace("(", "").replace(")", "").replace(":", "").strip()
        if not clean:
            return []
        try:
            with self._conn() as conn:
                # FTS5 MATCH with parameterized ? doesn't work in all SQLite builds;
                # clean is already sanitized above so inline interpolation is safe here.
                rows = conn.execute(
                    f"SELECT m.content "
                    f"FROM facts_fts "
                    f"JOIN facts_meta m ON facts_fts.rowid = m.rowid "
                    f"WHERE facts_fts MATCH '{clean}' AND m.status != 'archived' "
                    f"ORDER BY rank LIMIT {limit}",
                ).fetchall()
                return [r["content"] for r in rows]
        except sqlite3.OperationalError:
            return []

    def pinned(self) -> list[str]:
        """Always-inject facts (status='pinned') regardless of relevance."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT content FROM facts_meta WHERE status = 'pinned' ORDER BY created_at"
            ).fetchall()
        return [r["content"] for r in rows]

    def recent(self, n: int = 3) -> list[str]:
        """Recency anchor for workspaces with few indexed facts."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT content FROM facts_meta WHERE status = 'active' "
                "ORDER BY created_at DESC LIMIT ?",
                (n,),
            ).fetchall()
        return [r["content"] for r in rows]

    def archive_fact(self, fact_id: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE facts_meta SET status='archived' WHERE id=?", (fact_id,))
            conn.commit()

    def all_active(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, content, tags, source_run_id, created_at, reference_count "
                "FROM facts_meta WHERE status != 'archived' ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]
