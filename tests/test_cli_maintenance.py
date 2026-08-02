"""CLI coverage for local database maintenance operations."""

from __future__ import annotations

import asyncio
from pathlib import Path

from click.testing import CliRunner

from horizonx.cli import main
from horizonx.core.types import GoalNode
from horizonx.storage.sqlite import SqliteStore


def test_doctor_reports_schema_and_integrity(tmp_path: Path) -> None:
    path = tmp_path / "horizonx.db"

    result = CliRunner().invoke(main, ["--db", str(path), "doctor"])

    assert result.exit_code == 0
    assert "schema version" in result.output.lower()
    assert "integrity: ok" in result.output.lower()


def test_backup_and_restore_commands_preserve_data(tmp_path: Path) -> None:
    path = tmp_path / "horizonx.db"
    backup_path = tmp_path / "backups" / "horizonx.db"

    async def seed() -> None:
        store = SqliteStore(path)
        try:
            await store.save_goal(
                "run-one", GoalNode(id="g.root", name="Original", description="root")
            )
        finally:
            await store.close()

    asyncio.run(seed())
    runner = CliRunner()
    backup_result = runner.invoke(
        main, ["--db", str(path), "backup", str(backup_path)]
    )
    assert backup_result.exit_code == 0
    assert backup_path.is_file()

    async def mutate() -> None:
        store = SqliteStore(path)
        try:
            await store.save_goal(
                "run-one", GoalNode(id="g.root", name="Changed", description="root")
            )
        finally:
            await store.close()

    asyncio.run(mutate())
    restore_result = runner.invoke(
        main, ["--db", str(path), "restore", str(backup_path)]
    )
    assert restore_result.exit_code == 0

    async def load_name() -> str:
        store = SqliteStore(path)
        try:
            goal = await store.load_goal("run-one", "g.root")
            assert goal is not None
            return goal.name
        finally:
            await store.close()

    assert asyncio.run(load_name()) == "Original"


def test_checkpoint_command_completes(tmp_path: Path) -> None:
    path = tmp_path / "horizonx.db"

    result = CliRunner().invoke(main, ["--db", str(path), "checkpoint"])

    assert result.exit_code == 0
    assert "checkpoint complete" in result.output.lower()
