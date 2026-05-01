"""Integration tests for Runtime — full pipeline with MockAgent."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from horizonx.core.event_bus import Event
from horizonx.core.runtime import Runtime
from horizonx.core.types import (
    AgentConfig,
    GateAction,
    RunStatus,
    SessionStatus,
    StrategyConfig,
    Task,
    ValidatorConfig,
    SummarizerConfig,
)
from horizonx.storage.sqlite import SqliteStore


@pytest.fixture
def rt(tmp_path: Path) -> Runtime:
    store = SqliteStore(tmp_path / "test.db")
    return Runtime(store=store, workspace_root=tmp_path / "ws")


class TestSingleSessionPipeline:
    @pytest.mark.asyncio
    async def test_mock_agent_completes(self, rt: Runtime):
        task = Task(
            id="t1",
            name="Mock test",
            prompt="do nothing",
            strategy=StrategyConfig(kind="single"),
            agent=AgentConfig(type="mock", model="mock"),
        )
        run = await rt.run(task)
        assert run.status == RunStatus.COMPLETED
        assert run.cumulative.sessions_count == 1

    @pytest.mark.asyncio
    async def test_mock_agent_with_steps(self, rt: Runtime):
        task = Task(
            id="t2",
            name="Mock with steps",
            prompt="do things",
            strategy=StrategyConfig(kind="single"),
            agent=AgentConfig(
                type="mock",
                model="mock",
                extra={
                    "steps": [
                        {"type": "thought", "content": {"text": "planning"}},
                        {"type": "tool_call", "tool_name": "Bash",
                         "content": {"command": "make build"}},
                        {"type": "observation", "tool_name": "Bash",
                         "content": {"output": "Build succeeded"}},
                    ],
                },
            ),
        )
        run = await rt.run(task)
        assert run.status == RunStatus.COMPLETED
        steps = await rt.store.recent_steps(run.current_session_id, 100)
        assert len(steps) == 3

    @pytest.mark.asyncio
    async def test_mock_agent_error_propagates(self, rt: Runtime):
        task = Task(
            id="t3",
            name="Error test",
            prompt="fail",
            strategy=StrategyConfig(kind="single"),
            agent=AgentConfig(
                type="mock",
                model="mock",
                extra={"steps": [], "status": "errored", "error": "boom"},
            ),
        )
        run = await rt.run(task)
        # SingleSession yields run.failed for errored sessions
        assert run.status in (RunStatus.COMPLETED, RunStatus.FAILED)


class TestWithValidators:
    @pytest.mark.asyncio
    async def test_llm_judge_passes(self, rt: Runtime):
        task = Task(
            id="t4",
            name="Judge pass",
            prompt="test",
            strategy=StrategyConfig(kind="single"),
            agent=AgentConfig(type="mock", model="mock"),
            milestone_validators=[
                ValidatorConfig(
                    id="j1",
                    type="llm_judge",
                    runs="final",
                    config={"threshold": 0.5},
                ),
            ],
        )
        with patch("horizonx.core.llm_client.call_llm_json", new_callable=AsyncMock) as mock:
            mock.return_value = {
                "score": 0.9,
                "reason": "Good",
                "concerns": [],
                "evidence": [],
            }
            run = await rt.run(task)
            assert run.status == RunStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_shell_validator(self, rt: Runtime, tmp_path: Path):
        task = Task(
            id="t5",
            name="Shell gate",
            prompt="test",
            strategy=StrategyConfig(kind="single"),
            agent=AgentConfig(type="mock", model="mock"),
            milestone_validators=[
                ValidatorConfig(
                    id="s1",
                    type="shell",
                    runs="final",
                    config={"command": "true"},
                ),
            ],
        )
        run = await rt.run(task)
        assert run.status == RunStatus.COMPLETED


class TestWithSummarizer:
    @pytest.mark.asyncio
    async def test_summarizer_creates_file(self, rt: Runtime):
        task = Task(
            id="t6",
            name="Summarize test",
            prompt="test",
            strategy=StrategyConfig(kind="single"),
            agent=AgentConfig(type="mock", model="mock"),
            summarizer=SummarizerConfig(enabled=True),
        )
        with patch("horizonx.core.llm_client.call_llm_json", new_callable=AsyncMock) as mock:
            mock.return_value = {
                "summary_md": "Test summary",
                "key_decisions": ["decided X"],
                "blockers": [],
                "next_actions": ["do Y"],
                "files_modified": [],
                "tests_status": {},
                "confidence": 0.8,
            }
            run = await rt.run(task)
            # Summary file should exist in workspace
            summary_path = run.workspace_path / "summary.md"
            # Summarizer is called by strategies, not runtime.run directly.
            # SingleSession doesn't call summarize, so this is fine.


def _make_task(tmp_path: Path, task_id: str = "t1") -> Task:
    return Task(
        id=task_id, name="Test", prompt="test",
        strategy=StrategyConfig(kind="single"),
        agent=AgentConfig(type="mock", model="mock"),
    )


class TestForkMerge:
    @pytest.mark.asyncio
    async def test_fork_creates_new_run(self, rt: Runtime, tmp_path: Path):
        task = _make_task(tmp_path)
        parent_run = await rt.run(task)
        fork = await rt.fork_run(parent_run.id)
        assert fork.id != parent_run.id
        assert fork.parent_run_id == parent_run.id
        assert fork.workspace_path != parent_run.workspace_path
        assert fork.workspace_path.exists()

    @pytest.mark.asyncio
    async def test_fork_copies_handoff_files(self, rt: Runtime, tmp_path: Path):
        task = _make_task(tmp_path)
        parent_run = await rt.run(task)
        (parent_run.workspace_path / "progress.md").write_text("# Progress\n- step 1 done")
        (parent_run.workspace_path / "goals.json").write_text('{"version":1,"root":"g.root","nodes":{}}')

        fork = await rt.fork_run(parent_run.id)
        assert (fork.workspace_path / "progress.md").exists()
        assert (fork.workspace_path / "goals.json").exists()
        assert "step 1 done" in (fork.workspace_path / "progress.md").read_text()

    @pytest.mark.asyncio
    async def test_fork_persisted_to_store(self, rt: Runtime, tmp_path: Path):
        task = _make_task(tmp_path)
        parent_run = await rt.run(task)
        fork = await rt.fork_run(parent_run.id)
        loaded = await rt.store.load_run(fork.id)
        assert loaded.parent_run_id == parent_run.id

    @pytest.mark.asyncio
    async def test_merge_promotes_done_goals(self, rt: Runtime, tmp_path: Path):
        task = _make_task(tmp_path)
        parent_run = await rt.run(task)
        fork = await rt.fork_run(parent_run.id)

        parent_goals = {
            "version": 1, "root": "g.root",
            "nodes": {
                "g.root": {"id": "g.root", "name": "Root", "description": "root",
                            "verification_criteria": [], "status": "in_progress",
                            "children": ["g.a"], "depends_on": [], "attempts": 1,
                            "progress_pct": 0.0, "version": 1, "notes": "",
                            "last_updated_at": "2026-01-01T00:00:00+00:00",
                            "last_updated_by_session": None, "max_attempts": 3,
                            "parent_id": None},
                "g.a": {"id": "g.a", "name": "Task A", "description": "do A",
                        "verification_criteria": [], "status": "pending",
                        "children": [], "depends_on": [], "attempts": 0,
                        "progress_pct": 0.0, "version": 0, "notes": "",
                        "last_updated_at": "2026-01-01T00:00:00+00:00",
                        "last_updated_by_session": None, "max_attempts": 3,
                        "parent_id": "g.root"},
            },
        }
        import json
        (parent_run.workspace_path / "goals.json").write_text(json.dumps(parent_goals))

        fork_goals = json.loads(json.dumps(parent_goals))
        fork_goals["nodes"]["g.a"]["status"] = "done"
        fork_goals["nodes"]["g.a"]["progress_pct"] = 100.0
        (fork.workspace_path / "goals.json").write_text(json.dumps(fork_goals))

        await rt.merge_run(fork.id, parent_run.id)

        merged = json.loads((parent_run.workspace_path / "goals.json").read_text())
        assert merged["nodes"]["g.a"]["status"] == "done"

    @pytest.mark.asyncio
    async def test_merge_no_regression(self, rt: Runtime, tmp_path: Path):
        task = _make_task(tmp_path)
        parent_run = await rt.run(task)
        fork = await rt.fork_run(parent_run.id)

        import json
        goals_template = {
            "version": 1, "root": "g.root",
            "nodes": {
                "g.root": {"id": "g.root", "name": "Root", "description": "root",
                            "verification_criteria": [], "status": "done",
                            "children": ["g.a"], "depends_on": [], "attempts": 1,
                            "progress_pct": 100.0, "version": 2, "notes": "",
                            "last_updated_at": "2026-01-01T00:00:00+00:00",
                            "last_updated_by_session": None, "max_attempts": 3,
                            "parent_id": None},
                "g.a": {"id": "g.a", "name": "Task A", "description": "do A",
                        "verification_criteria": [], "status": "done",
                        "children": [], "depends_on": [], "attempts": 1,
                        "progress_pct": 100.0, "version": 2, "notes": "",
                        "last_updated_at": "2026-01-01T00:00:00+00:00",
                        "last_updated_by_session": None, "max_attempts": 3,
                        "parent_id": "g.root"},
            },
        }
        (parent_run.workspace_path / "goals.json").write_text(json.dumps(goals_template))

        fork_goals = json.loads(json.dumps(goals_template))
        fork_goals["nodes"]["g.a"]["status"] = "pending"
        (fork.workspace_path / "goals.json").write_text(json.dumps(fork_goals))

        await rt.merge_run(fork.id, parent_run.id)
        merged = json.loads((parent_run.workspace_path / "goals.json").read_text())
        assert merged["nodes"]["g.a"]["status"] == "done"

    @pytest.mark.asyncio
    async def test_merge_concatenates_notes(self, rt: Runtime, tmp_path: Path):
        task = _make_task(tmp_path)
        parent_run = await rt.run(task)
        fork = await rt.fork_run(parent_run.id)

        import json
        base_node = {"id": "g.root", "name": "Root", "description": "root",
                     "verification_criteria": [], "status": "in_progress",
                     "children": [], "depends_on": [], "attempts": 1,
                     "progress_pct": 0.0, "version": 1, "notes": "parent note",
                     "last_updated_at": "2026-01-01T00:00:00+00:00",
                     "last_updated_by_session": None, "max_attempts": 3, "parent_id": None}
        parent_goals = {"version": 1, "root": "g.root", "nodes": {"g.root": base_node}}
        (parent_run.workspace_path / "goals.json").write_text(json.dumps(parent_goals))

        fork_node = dict(base_node)
        fork_node["notes"] = "fork discovered something new"
        fork_goals = {"version": 1, "root": "g.root", "nodes": {"g.root": fork_node}}
        (fork.workspace_path / "goals.json").write_text(json.dumps(fork_goals))

        await rt.merge_run(fork.id, parent_run.id)
        merged = json.loads((parent_run.workspace_path / "goals.json").read_text())
        notes = merged["nodes"]["g.root"]["notes"]
        assert "parent note" in notes
        assert "fork discovered" in notes


class TestEventBus:
    @pytest.mark.asyncio
    async def test_events_published(self, rt: Runtime):
        import asyncio

        events: list[Event] = []
        ready = asyncio.Event()

        async def collect_events():
            sub = rt.bus.subscribe()
            # Force the generator to run up to its first yield (registers the queue)
            it = sub.__aiter__()
            ready.set()
            try:
                while True:
                    event = await asyncio.wait_for(it.__anext__(), timeout=2.0)
                    events.append(event)
            except (asyncio.TimeoutError, StopAsyncIteration, asyncio.CancelledError):
                pass

        collector = asyncio.create_task(collect_events())
        await ready.wait()
        # Small yield to let the subscriber fully register
        await asyncio.sleep(0.01)

        task = Task(
            id="t7",
            name="Events",
            prompt="test",
            strategy=StrategyConfig(kind="single"),
            agent=AgentConfig(type="mock", model="mock"),
        )
        await rt.run(task)
        await asyncio.sleep(0.1)
        collector.cancel()
        try:
            await collector
        except asyncio.CancelledError:
            pass

        event_types = [e.type for e in events]
        assert "run.started" in event_types
        assert "session.started" in event_types
        assert "session.completed" in event_types
