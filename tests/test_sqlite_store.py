"""Tests for SqliteStore — CRUD operations."""

from __future__ import annotations

from pathlib import Path

import pytest

from horizonx.core.types import (
    AgentConfig,
    GateAction,
    GateDecision,
    GoalNode,
    GoalStatus,
    Run,
    RunStatus,
    Session,
    SessionStatus,
    SpinReport,
    Step,
    StepType,
    StrategyConfig,
    Task,
)
from horizonx.storage.sqlite import SqliteStore


@pytest.fixture
def db(tmp_path: Path) -> SqliteStore:
    return SqliteStore(tmp_path / "test.db")


def _make_task() -> Task:
    return Task(
        id="t1",
        name="Test",
        prompt="test",
        strategy=StrategyConfig(kind="single"),
        agent=AgentConfig(type="mock", model="mock"),
    )


class TestRunCRUD:
    @pytest.mark.asyncio
    async def test_save_and_load(self, db: SqliteStore):
        run = Run(task=_make_task(), workspace_path=Path("/tmp/w"))
        await db.save_run(run)
        loaded = await db.load_run(run.id)
        assert loaded.id == run.id
        assert loaded.status == RunStatus.PENDING

    @pytest.mark.asyncio
    async def test_update_status(self, db: SqliteStore):
        run = Run(task=_make_task(), workspace_path=Path("/tmp/w"))
        await db.save_run(run)
        run.status = RunStatus.COMPLETED
        await db.save_run(run)
        loaded = await db.load_run(run.id)
        assert loaded.status == RunStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_list_runs(self, db: SqliteStore):
        for i in range(3):
            run = Run(task=_make_task(), workspace_path=Path(f"/tmp/w{i}"))
            await db.save_run(run)
        runs = await db.list_runs()
        assert len(runs) == 3

    @pytest.mark.asyncio
    async def test_load_nonexistent(self, db: SqliteStore):
        with pytest.raises(KeyError):
            await db.load_run("nonexistent")


class TestSessionCRUD:
    @pytest.mark.asyncio
    async def test_save_session(self, db: SqliteStore):
        session = Session(run_id="r1", sequence_index=0)
        await db.save_session(session)

    @pytest.mark.asyncio
    async def test_update_session(self, db: SqliteStore):
        session = Session(run_id="r1", sequence_index=0)
        await db.save_session(session)
        session.status = SessionStatus.COMPLETED
        session.steps_count = 42
        await db.save_session(session)


class TestStepCRUD:
    @pytest.mark.asyncio
    async def test_save_and_recent(self, db: SqliteStore):
        for i in range(10):
            step = Step(
                session_id="s1",
                sequence=i,
                type=StepType.TOOL_CALL,
                tool_name="Bash",
                content={"command": f"echo {i}"},
            )
            await db.save_step(step)
        recent = await db.recent_steps("s1", 5)
        assert len(recent) == 5
        assert recent[0].sequence == 5
        assert recent[-1].sequence == 9

    @pytest.mark.asyncio
    async def test_recent_steps_empty(self, db: SqliteStore):
        recent = await db.recent_steps("nonexistent", 10)
        assert recent == []


class TestGoalCRUD:
    @pytest.mark.asyncio
    async def test_save_and_load(self, db: SqliteStore):
        goal = GoalNode(id="g.test", name="Test goal", description="desc")
        await db.save_goal("r1", goal)
        loaded = await db.load_goal("r1", "g.test")
        assert loaded is not None
        assert loaded.name == "Test goal"

    @pytest.mark.asyncio
    async def test_load_nonexistent(self, db: SqliteStore):
        loaded = await db.load_goal("r1", "g.nope")
        assert loaded is None

    @pytest.mark.asyncio
    async def test_update_goal(self, db: SqliteStore):
        goal = GoalNode(id="g.test", name="Test", description="desc")
        await db.save_goal("r1", goal)
        goal.status = GoalStatus.DONE
        goal.attempts = 2
        await db.save_goal("r1", goal)
        loaded = await db.load_goal("r1", "g.test")
        assert loaded.status == GoalStatus.DONE
        assert loaded.attempts == 2


class TestValidationCRUD:
    @pytest.mark.asyncio
    async def test_save_validation(self, db: SqliteStore):
        run = Run(task=_make_task(), workspace_path=Path("/tmp/w"))
        session = Session(run_id=run.id, sequence_index=0)
        decision = GateDecision(
            decision=GateAction.CONTINUE,
            reason="OK",
            score=0.9,
            validator_name="test",
        )
        await db.save_validation(run, session, decision)

    @pytest.mark.asyncio
    async def test_recent_validator_scores(self, db: SqliteStore):
        run = Run(task=_make_task(), workspace_path=Path("/tmp/w"))
        session = Session(run_id=run.id, sequence_index=0)
        for score in [0.5, 0.7, 0.8]:
            decision = GateDecision(
                decision=GateAction.CONTINUE,
                reason="OK",
                score=score,
                validator_name="test",
            )
            await db.save_validation(run, session, decision)
        scores = await db.recent_validator_scores(run.id, 3)
        assert len(scores) == 3


class TestSpinReportCRUD:
    @pytest.mark.asyncio
    async def test_save_spin_report(self, db: SqliteStore):
        session = Session(run_id="r1", sequence_index=0)
        report = SpinReport(
            detected=True,
            layer="exact_loop",
            detail={"hash": "abc", "count": 5},
            action="terminate_session_and_retry",
        )
        await db.save_spin_report(session, report)
