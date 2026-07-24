"""Tests for HX-13: WorkspaceKnowledgeStore and KnowledgeHandoffDir."""
from horizonx.memory.handoff import KnowledgeHandoffDir, _parse_fact_file
from horizonx.memory.knowledge_store import WorkspaceKnowledgeStore


def test_upsert_and_search(tmp_path):
    store = WorkspaceKnowledgeStore(tmp_path / "knowledge.db")
    store.upsert_fact("PyJWT 2.8 requires explicit algorithm in decode()", tags=["jwt", "auth"])
    results = store.search("jwt decode algorithm")
    assert len(results) == 1
    assert "PyJWT" in results[0]


def test_search_empty_returns_empty(tmp_path):
    store = WorkspaceKnowledgeStore(tmp_path / "knowledge.db")
    assert store.search("") == []
    assert store.search("   ") == []


def test_pinned_facts(tmp_path):
    store = WorkspaceKnowledgeStore(tmp_path / "knowledge.db")
    fid = store.upsert_fact("Always use UTC timestamps", tags=["conventions"])
    # Pin it via the store's own context manager to avoid FTS5 trigger conflict
    import sqlite3
    # Use a fresh connection outside any active FTS5 read transaction
    conn = sqlite3.connect(store.db_path)
    try:
        conn.execute("UPDATE facts_meta SET status='pinned' WHERE id=?", (fid,))
        conn.commit()
    finally:
        conn.close()
    assert "UTC" in store.pinned()[0]
    assert store.search("something else") == []  # pinned not returned by search


def test_recent(tmp_path):
    store = WorkspaceKnowledgeStore(tmp_path / "knowledge.db")
    store.upsert_fact("fact one")
    store.upsert_fact("fact two")
    recent = store.recent(1)
    assert len(recent) == 1
    assert "fact two" in recent[0]


def test_for_run_path_is_relative_to_workspace(tmp_path):
    ws = tmp_path / "horizonx-workspaces" / "my-task-abc12345"
    ws.mkdir(parents=True)
    store = WorkspaceKnowledgeStore.for_run(ws, workspace_id="ws-proj-x")
    expected = ws.parent / ".horizonx" / "ws-proj-x" / "knowledge.db"
    assert store.db_path == expected


def test_for_run_default_workspace_id(tmp_path):
    ws = tmp_path / "horizonx-workspaces" / "task-xyz"
    ws.mkdir(parents=True)
    store = WorkspaceKnowledgeStore.for_run(ws)
    assert "default" in str(store.db_path)


def test_parse_fact_file_with_frontmatter(tmp_path):
    f = tmp_path / "auth.md"
    f.write_text("---\ntags: [jwt, auth, python]\n---\nAlways validate JWT expiry.\n")
    content, tags = _parse_fact_file(f)
    assert "JWT expiry" in content
    assert "jwt" in tags
    assert "auth" in tags


def test_handoff_dir_sync(tmp_path):
    ws = tmp_path / "workspace"
    kdir = ws / "knowledge"
    kdir.mkdir(parents=True)
    (kdir / "auth.md").write_text("---\ntags: [security]\n---\nUse bcrypt for password hashing.\n")
    (kdir / "db.md").write_text("Always use parameterised queries to prevent SQL injection.")

    store = WorkspaceKnowledgeStore(tmp_path / "knowledge.db")
    count = KnowledgeHandoffDir(ws).sync(store, run_id="run-1")
    assert count == 2
    results = store.search("password hashing")
    assert len(results) >= 1


def test_handoff_dir_empty_knowledge_dir(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    store = WorkspaceKnowledgeStore(tmp_path / "knowledge.db")
    count = KnowledgeHandoffDir(ws).sync(store)
    assert count == 0
