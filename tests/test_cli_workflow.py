"""CLI coverage for initializing and using a local HorizonX project."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner
from pydantic import ValidationError

from horizonx import AttemptRecord, CumulativeMetrics, Run, RunStatus, Session, Task
from horizonx.cli import main
from horizonx.core.event_bus import Event
from horizonx.core.operator_commands import OperatorCommand, OperatorCommandKind
from horizonx.project import ProjectConfig
from horizonx.storage import SqliteStore
from horizonx.storage.sqlite import StoreError


def _seed_run(
    db_path: Path, *, status: RunStatus = RunStatus.RUNNING, agent_type: str = "mock"
) -> Run:
    async def seed() -> Run:
        store = SqliteStore(db_path)
        run = Run(
            id="durable-run",
            task=Task.model_validate(
                {
                    "id": "task",
                    "name": "Durable task",
                    "prompt": "do it",
                    "strategy": {"kind": "single"},
                    "agent": {"type": agent_type, "model": "mock"},
                }
            ),
            status=status,
            workspace_path=db_path.parent / "workspace",
            cumulative=CumulativeMetrics(usd=1.25),
        )
        await store.save_run(run)
        await store.close()
        return run

    import asyncio

    return asyncio.run(seed())


def test_init_creates_valid_config_and_runnable_mock_task(tmp_path: Path) -> None:
    project_dir = tmp_path / "new-project"

    result = CliRunner().invoke(main, ["init", str(project_dir)])

    assert result.exit_code == 0, result.output
    config_path = project_dir / "horizonx.yaml"
    task_path = project_dir / "tasks" / "example.yaml"
    assert config_path.is_file()
    assert task_path.is_file()

    config = ProjectConfig.load(config_path)
    assert config.version == 1
    assert config.db_path == project_dir / "horizonx.db"
    assert config.workspace_root == project_dir / "horizonx-workspaces"
    task = Task.model_validate(yaml.safe_load(task_path.read_text()))
    assert task.agent.type == "mock"


def test_project_config_rejects_unknown_fields_and_resolves_relative_paths(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "horizonx.yaml"
    config_path.write_text(
        "version: 1\ndb_path: data/runs.db\nworkspace_root: workspaces\nunknown: no\n"
    )

    try:
        ProjectConfig.load(config_path)
    except ValidationError as exc:
        assert "unknown" in str(exc)
    else:
        raise AssertionError("unknown configuration fields must be rejected")

    config_path.write_text("version: 1\ndb_path: data/runs.db\nworkspace_root: workspaces\n")
    config = ProjectConfig.load(config_path)
    assert config.db_path == tmp_path / "data" / "runs.db"
    assert config.workspace_root == tmp_path / "workspaces"


def test_init_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    config_path = project_dir / "horizonx.yaml"
    config_path.write_text("keep me")

    runner = CliRunner()
    result = runner.invoke(main, ["init", str(project_dir)])

    assert result.exit_code != 0
    assert "--force" in result.output
    assert config_path.read_text() == "keep me"

    forced = runner.invoke(main, ["init", "--force", str(project_dir)])
    assert forced.exit_code == 0, forced.output
    assert ProjectConfig.load(config_path).version == 1


def test_force_init_repairs_malformed_config_in_current_directory(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "horizonx.yaml").write_text("version: not-a-version\n")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["init", "--force", "."])

    assert result.exit_code == 0, result.output
    assert ProjectConfig.load(tmp_path / "horizonx.yaml").version == 1


def test_initialized_example_runs_with_the_mock_provider(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    runner = CliRunner()

    initialized = runner.invoke(main, ["init", str(project_dir)])
    result = runner.invoke(main, ["run", str(project_dir / "tasks" / "example.yaml")])

    assert initialized.exit_code == 0, initialized.output
    assert result.exit_code == 0, result.output
    assert "status: completed" in result.output.lower()
    assert "Provider session: mock mock-session-001" in result.output
    assert "Cost: unknown" in result.output
    assert "Attach: horizonx attach " in result.output


def test_status_prints_compact_durable_summary(tmp_path: Path) -> None:
    db_path = tmp_path / "horizonx.db"
    run = _seed_run(db_path)

    result = CliRunner().invoke(main, ["--db", str(db_path), "status", run.id])

    assert result.exit_code == 0, result.output
    assert "Status: running" in result.output
    assert f"Workspace: {run.workspace_path}" in result.output
    assert "Cost: $1.25" in result.output
    assert "Provider session: not recorded" in result.output


def test_attach_reports_workspace_without_claiming_process_reattachment(tmp_path: Path) -> None:
    db_path = tmp_path / "horizonx.db"
    run = _seed_run(db_path)

    result = CliRunner().invoke(main, ["--db", str(db_path), "attach", run.id])

    assert result.exit_code == 0, result.output
    assert str(run.workspace_path) in result.output
    assert "no provider session is recorded" in result.output.lower()
    assert "cannot reattach" in result.output.lower()


@pytest.mark.parametrize(
    ("provider", "expected_hint"),
    [("claude_code", "claude --resume provider-session"), ("codex", "codex exec resume provider-session")],
)
def test_attach_prints_provider_specific_resume_hint(
    tmp_path: Path, provider: str, expected_hint: str
) -> None:
    db_path = tmp_path / "horizonx.db"
    run = _seed_run(db_path, agent_type=provider)

    async def add_session() -> None:
        store = SqliteStore(db_path)
        await store.save_session(
            Session(run_id=run.id, sequence_index=1, agent_session_id="provider-session")
        )
        await store.close()

    import asyncio

    asyncio.run(add_session())
    result = CliRunner().invoke(main, ["--db", str(db_path), "attach", run.id])

    assert result.exit_code == 0, result.output
    assert expected_hint in result.output


def test_attach_uses_latest_recorded_attempt_session_not_latest_null_attempt(tmp_path: Path) -> None:
    db_path = tmp_path / "horizonx.db"
    run = _seed_run(db_path, agent_type="codex")

    async def add_attempts() -> None:
        store = SqliteStore(db_path)
        for ordinal, provider_session_id in ((1, "saved-session"), (2, None)):
            session = Session(run_id=run.id, sequence_index=ordinal)
            await store.save_session(session)
            await store.create_attempt(
                AttemptRecord(
                    run_id=run.id, session_id=session.id, provider="codex", model="mock",
                    workspace_path=run.workspace_path, ordinal=ordinal,
                    provider_session_id=provider_session_id,
                )
            )
        await store.close()

    import asyncio

    asyncio.run(add_attempts())
    result = CliRunner().invoke(main, ["--db", str(db_path), "attach", run.id])

    assert result.exit_code == 0, result.output
    assert "codex exec resume saved-session" in result.output


def test_evidence_exports_durable_run_bundle_as_json(tmp_path: Path) -> None:
    db_path = tmp_path / "horizonx.db"
    run = _seed_run(db_path)

    async def add_event() -> None:
        store = SqliteStore(db_path)
        await store.append_event(Event(type="run.started", run_id=run.id))
        await store.close()

    import asyncio

    asyncio.run(add_event())
    result = CliRunner().invoke(main, ["--db", str(db_path), "evidence", run.id])

    assert result.exit_code == 0, result.output
    bundle = json.loads(result.output)
    assert bundle["run"]["id"] == run.id
    assert bundle["events"][0]["type"] == "run.started"
    assert {"goals", "validations", "sessions", "attempts", "spins"} <= bundle.keys()
    assert "spin_reports" not in bundle


def test_steer_persists_command_and_reports_idempotent_duplicate(tmp_path: Path) -> None:
    db_path = tmp_path / "horizonx.db"
    run = _seed_run(db_path)
    runner = CliRunner()

    accepted = runner.invoke(
        main,
        ["--db", str(db_path), "steer", run.id, "Focus on tests", "--idempotency-key", "steer-1"],
    )
    duplicate = runner.invoke(
        main,
        ["--db", str(db_path), "steer", run.id, "Focus on tests", "--idempotency-key", "steer-1"],
    )

    assert accepted.exit_code == 0, accepted.output
    assert "accepted" in accepted.output.lower()
    assert duplicate.exit_code == 0, duplicate.output
    assert "duplicate" in duplicate.output.lower()


def test_cancel_uses_durable_idempotent_submission(tmp_path: Path) -> None:
    db_path = tmp_path / "horizonx.db"
    run = _seed_run(db_path)
    runner = CliRunner()

    accepted = runner.invoke(main, ["--db", str(db_path), "cancel", run.id])
    duplicate = runner.invoke(main, ["--db", str(db_path), "cancel", run.id])

    assert accepted.exit_code == 0, accepted.output
    assert "accepted" in accepted.output.lower()
    assert duplicate.exit_code == 0, duplicate.output
    assert "duplicate" in duplicate.output.lower()


@pytest.mark.parametrize(
    "command",
    [["resume", "missing"], ["watch", "missing", "--interval", "0"], ["steer", "missing", "focus"], ["cancel", "missing"]],
)
def test_operator_commands_report_missing_runs(tmp_path: Path, command: list[str]) -> None:
    result = CliRunner().invoke(main, ["--db", str(tmp_path / "horizonx.db"), *command])

    assert result.exit_code != 0
    assert "run not found: missing" in result.output


@pytest.mark.parametrize("command", [["steer", "durable-run", "focus"], ["cancel", "durable-run"]])
def test_operator_commands_reject_terminal_runs(tmp_path: Path, command: list[str]) -> None:
    db_path = tmp_path / "horizonx.db"
    _seed_run(db_path, status=RunStatus.COMPLETED)

    result = CliRunner().invoke(main, ["--db", str(db_path), *command])

    assert result.exit_code != 0
    assert "terminal" in result.output.lower()


def test_atomic_steer_rejects_terminal_run_without_persisting_command(tmp_path: Path) -> None:
    db_path = tmp_path / "horizonx.db"
    run = _seed_run(db_path, status=RunStatus.COMPLETED)

    async def submit() -> list[OperatorCommand]:
        store = SqliteStore(db_path)
        try:
            with pytest.raises(StoreError, match="terminal"):
                await store.submit_steer_command(
                    OperatorCommand(
                        run_id=run.id, kind=OperatorCommandKind.STEER, actor="test",
                        instruction="too late", idempotency_key="terminal-steer",
                    )
                )
            return await store.list_operator_commands(run.id)
        finally:
            await store.close()

    import asyncio

    assert asyncio.run(submit()) == []


def test_cli_uses_database_path_from_config_in_current_directory(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / "horizonx.yaml"
    config_path.write_text("version: 1\ndb_path: state/project.db\nworkspace_root: workspaces\n")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["doctor"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "state" / "project.db").is_file()
    assert not (tmp_path / "horizonx.db").exists()


def test_explicit_database_option_takes_precedence_over_project_config(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "horizonx.yaml").write_text(
        "version: 1\ndb_path: state/project.db\nworkspace_root: workspaces\n"
    )
    explicit_db = tmp_path / "override.db"
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["--db", str(explicit_db), "doctor"])

    assert result.exit_code == 0, result.output
    assert explicit_db.is_file()
    assert not (tmp_path / "state" / "project.db").exists()


def test_fork_uses_project_workspace_root_and_closes_store(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "horizonx.yaml").write_text(
        "version: 1\ndb_path: state/project.db\nworkspace_root: custom-workspaces\n"
    )
    monkeypatch.chdir(tmp_path)
    captured: dict[str, object] = {}

    class StubStore:
        def __init__(self, path: Path) -> None:
            captured["db_path"] = path

        async def close(self) -> None:
            captured["closed"] = True

    class StubRuntime:
        def __init__(self, *, store: StubStore, workspace_root: Path | None = None) -> None:
            captured["workspace_root"] = workspace_root

        async def fork_run(
            self, run_id: str, strategy_override: dict[str, object] | None = None
        ) -> SimpleNamespace:
            return SimpleNamespace(id="forked-run", workspace_path=tmp_path / "forked")

    with (
        patch("horizonx.cli.SqliteStore", StubStore),
        patch("horizonx.cli.Runtime", StubRuntime),
    ):
        result = CliRunner().invoke(main, ["fork", "source-run"])

    assert result.exit_code == 0, result.output
    assert captured["workspace_root"] == tmp_path / "custom-workspaces"
    assert captured["closed"] is True


def test_fork_uses_legacy_workspace_default_without_project_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    captured: dict[str, object] = {}

    class StubStore:
        def __init__(self, path: Path) -> None:
            pass

        async def close(self) -> None:
            pass

    class StubRuntime:
        def __init__(self, *, store: StubStore, workspace_root: Path) -> None:
            captured["workspace_root"] = workspace_root

        async def fork_run(
            self, run_id: str, strategy_override: dict[str, object] | None = None
        ) -> SimpleNamespace:
            return SimpleNamespace(id="forked-run", workspace_path=tmp_path / "forked")

    with (
        patch("horizonx.cli.SqliteStore", StubStore),
        patch("horizonx.cli.Runtime", StubRuntime),
    ):
        result = CliRunner().invoke(main, ["fork", "source-run"])

    assert result.exit_code == 0, result.output
    assert captured["workspace_root"] == Path("horizonx-workspaces")


def test_fork_rejects_malformed_mutation_before_creating_resources(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    constructed = False

    class StubStore:
        def __init__(self, path: Path) -> None:
            nonlocal constructed
            constructed = True

    with patch("horizonx.cli.SqliteStore", StubStore):
        result = CliRunner().invoke(main, ["fork", "source-run", "--mutation", "{"])

    assert result.exit_code != 0
    assert constructed is False


def test_resume_uses_snapshot_project_workspace_and_closes_resources(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "horizonx.yaml").write_text(
        "version: 1\ndb_path: state/project.db\nworkspace_root: custom-workspaces\n"
    )
    monkeypatch.chdir(tmp_path)
    snapshot = _seed_run(tmp_path / "state" / "project.db")
    captured: dict[str, object] = {}

    class StubStore:
        def __init__(self, path: Path) -> None:
            captured["db"] = path

        async def load_run(self, run_id: str) -> Run:
            return snapshot

        async def close(self) -> None:
            captured["store_closed"] = True

    class StubRuntime:
        def __init__(self, *, store: StubStore, workspace_root: Path) -> None:
            captured["workspace_root"] = workspace_root

        async def run(self, task: Task, *, resume_from: str) -> Run:
            captured["task"] = task
            captured["resume_from"] = resume_from
            return snapshot

        async def shutdown(self, *, close_store: bool) -> None:
            captured["runtime_closed"] = True

    with patch("horizonx.cli.SqliteStore", StubStore), patch("horizonx.cli.Runtime", StubRuntime):
        result = CliRunner().invoke(main, ["resume", snapshot.id])

    assert result.exit_code == 0, result.output
    assert captured["workspace_root"] == tmp_path / "custom-workspaces"
    assert captured["resume_from"] == snapshot.id
    assert captured["store_closed"] is True and captured["runtime_closed"] is True


@pytest.mark.parametrize("status", [RunStatus.COMPLETED])
def test_resume_rejects_terminal_snapshot(tmp_path: Path, status: RunStatus) -> None:
    db_path = tmp_path / "horizonx.db"
    run = _seed_run(db_path, status=status)

    result = CliRunner().invoke(main, ["--db", str(db_path), "resume", run.id])

    assert result.exit_code != 0
    assert "terminal" in result.output.lower()


def test_watch_prints_durable_events_and_exits_on_terminal_status(tmp_path: Path) -> None:
    db_path = tmp_path / "horizonx.db"
    run = _seed_run(db_path, status=RunStatus.COMPLETED)

    async def add_event() -> None:
        store = SqliteStore(db_path)
        await store.append_event(Event(type="run.completed", run_id=run.id))
        await store.close()

    import asyncio

    asyncio.run(add_event())
    result = CliRunner().invoke(main, ["--db", str(db_path), "watch", run.id, "--interval", "0"])

    assert result.exit_code == 0, result.output
    assert "[run.completed]" in result.output
    assert "Status: completed" in result.output


def test_watch_observes_event_after_polling_starts_then_closes_store(tmp_path: Path) -> None:
    run = _seed_run(tmp_path / "seed.db")
    captured: dict[str, object] = {"loads": 0, "events": 0}

    class StubStore:
        def __init__(self, path: Path) -> None:
            pass

        async def load_run(self, run_id: str) -> Run:
            captured["loads"] = int(captured["loads"]) + 1
            return run.model_copy(
                update={"status": RunStatus.RUNNING if captured["loads"] == 1 else RunStatus.COMPLETED}
            )

        async def list_events(self, run_id: str, *, after_sequence: int | None = None) -> list[Event]:
            captured["events"] = int(captured["events"]) + 1
            return [] if captured["events"] == 1 else [
                Event(sequence=1, type="run.completed", run_id=run_id, payload={"durable": True})
            ]

        async def close(self) -> None:
            captured["closed"] = True

    with patch("horizonx.cli.SqliteStore", StubStore):
        result = CliRunner().invoke(main, ["watch", run.id, "--interval", "0"])

    assert result.exit_code == 0, result.output
    assert "[run.completed]" in result.output
    assert captured["closed"] is True


def test_config_directory_is_reported_as_invalid_config(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "horizonx.yaml").mkdir()
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["doctor"])

    assert result.exit_code != 0
    assert "invalid horizonx.yaml" in result.output.lower()


@pytest.mark.parametrize("db_path", ["", "database-directory"])
def test_project_config_rejects_blank_or_directory_database_path(
    tmp_path: Path, db_path: str
) -> None:
    if db_path == "database-directory":
        (tmp_path / db_path).mkdir()
    config_path = tmp_path / "horizonx.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {"version": 1, "db_path": db_path, "workspace_root": "workspaces"}
        )
    )

    with pytest.raises(ValidationError, match="db_path"):
        ProjectConfig.load(config_path)
