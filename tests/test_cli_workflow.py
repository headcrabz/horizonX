"""CLI coverage for initializing and using a local HorizonX project."""

from __future__ import annotations

from pathlib import Path

import yaml
from click.testing import CliRunner
from pydantic import ValidationError

from horizonx import Task
from horizonx.cli import main
from horizonx.project import ProjectConfig


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


def test_initialized_example_runs_with_the_mock_provider(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    runner = CliRunner()

    initialized = runner.invoke(main, ["init", str(project_dir)])
    result = runner.invoke(main, ["run", str(project_dir / "tasks" / "example.yaml")])

    assert initialized.exit_code == 0, initialized.output
    assert result.exit_code == 0, result.output
    assert "status: completed" in result.output.lower()


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
