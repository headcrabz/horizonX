"""Per-run FTS5 decision store for relevance-ranked context injection."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path


class RunKnowledgeStore:
    """SQLite FTS5 index over a run's decisions.jsonl for relevance search."""

    def __init__(self, workspace: Path) -> None:
        self.db_path = workspace / "knowledge.db"
        self._init()

    def _init(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS decisions_fts
                USING fts5(content, goal_id, ts, tokenize='unicode61')
            """)
            conn.commit()

    def index_decisions(self, jsonl_path: Path) -> None:
        """Re-index all entries from decisions.jsonl into FTS5."""
        if not jsonl_path.exists():
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM decisions_fts")
            for line in jsonl_path.read_text().splitlines():
                try:
                    obj = json.loads(line)
                    content = obj.get("decision", "") + " " + obj.get("rationale", "")
                    goal_id = obj.get("goal", "")
                    ts = obj.get("ts", "")
                    conn.execute(
                        "INSERT INTO decisions_fts(content, goal_id, ts) VALUES (?, ?, ?)",
                        (content, goal_id, ts),
                    )
                except (json.JSONDecodeError, KeyError):
                    continue
            conn.commit()

    def search(self, query: str, limit: int = 12) -> list[str]:
        """Return raw JSON lines matching query, ranked by FTS5 relevance."""
        if not self.db_path.exists():
            return []
        clean_query = query.replace('"', "").replace("(", "").replace(")", "").strip()
        if not clean_query:
            return []
        try:
            with sqlite3.connect(self.db_path) as conn:
                rows = conn.execute(
                    "SELECT content FROM decisions_fts WHERE decisions_fts MATCH ? ORDER BY rank LIMIT ?",
                    (clean_query, limit),
                ).fetchall()
            return [r[0] for r in rows]
        except sqlite3.OperationalError:
            return []

    def recent(self, n: int = 5) -> list[str]:
        """Return the n most recently indexed entries as recency anchor."""
        if not self.db_path.exists():
            return []
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT content FROM decisions_fts ORDER BY rowid DESC LIMIT ?", (n,)
            ).fetchall()
        return [r[0] for r in rows]
