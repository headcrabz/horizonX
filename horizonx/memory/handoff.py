"""Syncs agent-written knowledge files to the global WorkspaceKnowledgeStore."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from horizonx.memory.knowledge_store import WorkspaceKnowledgeStore


def _parse_fact_file(path: Path) -> tuple[str, list[str]]:
    """Parse a knowledge .md file with optional YAML frontmatter for tags.

    Supports:
        ---
        tags: [auth, jwt, security]
        ---
        Fact content here.
    """
    text = path.read_text(encoding="utf-8").strip()
    tags: list[str] = []
    content = text

    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            fm = parts[1].strip()
            content = parts[2].strip()
            for line in fm.splitlines():
                if line.startswith("tags:"):
                    raw = line.split(":", 1)[1].strip().strip("[]")
                    tags = [t.strip() for t in raw.split(",") if t.strip()]

    return content, tags


class KnowledgeHandoffDir:
    """Reads workspace/knowledge/*.md and syncs to the global knowledge store."""

    def __init__(self, workspace: Path) -> None:
        self.knowledge_dir = workspace / "knowledge"

    def sync(
        self,
        store: WorkspaceKnowledgeStore,
        run_id: str | None = None,
        goal_id: str | None = None,
    ) -> int:
        """Index all .md files in the knowledge dir. Returns count of facts indexed."""
        if not self.knowledge_dir.exists():
            return 0
        count = 0
        for md_file in sorted(self.knowledge_dir.glob("*.md")):
            try:
                content, tags = _parse_fact_file(md_file)
                if content:
                    store.upsert_fact(content, tags=tags, source_run_id=run_id, source_goal_id=goal_id)
                    count += 1
            except Exception:
                pass
        return count
