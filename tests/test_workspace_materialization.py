"""Workspace preparation contracts for repository-backed runs."""

from __future__ import annotations

import asyncio
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from horizonx.cli import main
from horizonx.core.runtime import Runtime
from horizonx.core.types import (
    AgentConfig,
    EnvironmentConfig,
    RepositoryConfig,
    RunStatus,
    StrategyConfig,
    Task,
)
from horizonx.environments.base import SetupCommandError, WorkspaceContainmentError
from horizonx.environments.git import GitWorktreeBackend
from horizonx.storage.sqlite import SqliteStore


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "tests@horizonx.local")
    _git(repo, "config", "user.name", "HorizonX Tests")
    (repo / "tracked.txt").write_text("source\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "Create source fixture")
    return repo


def _task(repository: RepositoryConfig, environment: EnvironmentConfig | None = None) -> Task:
    return Task(
        id="workspace-contract",
        name="Workspace contract",
        prompt="Inspect the prepared repository",
        strategy=StrategyConfig(kind="single"),
        agent=AgentConfig(type="mock", model="mock"),
        repository=repository,
        environment=environment or EnvironmentConfig(),
    )


def test_unimplemented_environment_backends_are_rejected() -> None:
    with pytest.raises(ValidationError):
        EnvironmentConfig(type="docker")  # type: ignore[arg-type]


def test_cli_repo_option_runs_in_an_isolated_worktree(tmp_path: Path) -> None:
    source = _repository(tmp_path)
    source_status = _git(source, "status", "--porcelain")
    task_path = tmp_path / "task.yaml"
    task_path.write_text(
        """\
id: cli-workspace
name: CLI workspace
prompt: Inspect the repository
strategy: {kind: single}
agent: {type: mock, model: mock}
"""
    )
    database = tmp_path / "horizonx.db"
    workspace_root = tmp_path / "workspaces"

    result = CliRunner().invoke(
        main,
        [
            "--db",
            str(database),
            "run",
            str(task_path),
            "--repo",
            str(source),
            "--workspace-root",
            str(workspace_root),
        ],
    )

    assert result.exit_code == 0, result.output
    workspaces = [path for path in workspace_root.iterdir() if path.is_dir()]
    assert len(workspaces) == 1
    assert (workspaces[0] / "tracked.txt").read_text() == "source\n"
    assert (workspaces[0] / ".horizonx" / "workspace.json").is_file()
    assert _git(source, "status", "--porcelain") == source_status


def test_cli_default_workspace_for_local_repo_is_outside_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _repository(tmp_path)
    task_path = tmp_path / "task.yaml"
    task_path.write_text(
        """\
id: cli-default-workspace
name: CLI default workspace
prompt: Inspect the repository
strategy: {kind: single}
agent: {type: mock, model: mock}
"""
    )
    monkeypatch.chdir(source)

    result = CliRunner().invoke(
        main,
        ["--db", str(tmp_path / "default.db"), "run", str(task_path), "--repo", "."],
    )

    assert result.exit_code == 0, result.output
    managed_root = tmp_path / ".source-horizonx-workspaces"
    assert len([path for path in managed_root.iterdir() if path.is_dir()]) == 1
    assert _git(source, "status", "--porcelain") == ""


def test_cli_reports_setup_command_failure(tmp_path: Path) -> None:
    source = _repository(tmp_path)
    task_path = tmp_path / "failing-task.yaml"
    task_path.write_text(
        """\
id: cli-setup-failure
name: CLI setup failure
prompt: Inspect the repository
strategy: {kind: single}
agent: {type: mock, model: mock}
environment:
  type: local
  setup_commands: ["exit 29"]
"""
    )

    result = CliRunner().invoke(
        main,
        [
            "--db",
            str(tmp_path / "failure.db"),
            "run",
            str(task_path),
            "--repo",
            str(source),
            "--workspace-root",
            str(tmp_path / "failure-workspaces"),
        ],
    )

    assert result.exit_code == 1
    assert "setup command failed with exit 29" in result.output


@pytest.mark.asyncio
async def test_local_repository_is_materialized_as_an_isolated_worktree(
    tmp_path: Path,
) -> None:
    source = _repository(tmp_path)
    (source / "tracked.txt").write_text("uncommitted source edit\n")
    source_status = _git(source, "status", "--porcelain")
    backend = GitWorktreeBackend(tmp_path / "workspaces", EnvironmentConfig())

    prepared = await backend.prepare(
        "run-isolated", RepositoryConfig(path=source, ref="HEAD")
    )

    assert prepared.path == (tmp_path / "workspaces" / "run-isolated").resolve()
    assert prepared.path != source
    assert (prepared.path / "tracked.txt").read_text() == "source\n"
    assert prepared.metadata.source_commit == _git(source, "rev-parse", "HEAD")
    assert prepared.metadata.backend == "local_git_worktree"
    assert _git(source, "status", "--porcelain") == source_status
    assert (prepared.path / ".horizonx" / "workspace.json").is_file()


@pytest.mark.asyncio
async def test_clone_url_is_materialized_at_the_requested_ref(tmp_path: Path) -> None:
    source = _repository(tmp_path)
    backend = GitWorktreeBackend(tmp_path / "workspaces", EnvironmentConfig())

    prepared = await backend.prepare(
        "run-clone", RepositoryConfig(url=source.as_uri(), ref="HEAD")
    )

    assert (prepared.path / "tracked.txt").read_text() == "source\n"
    assert prepared.metadata.source_kind == "clone_url"
    assert prepared.metadata.source_commit == _git(source, "rev-parse", "HEAD")


@pytest.mark.asyncio
async def test_requested_target_branch_is_created_only_in_run_worktree(
    tmp_path: Path,
) -> None:
    source = _repository(tmp_path)
    source_branch = _git(source, "rev-parse", "--abbrev-ref", "HEAD")
    backend = GitWorktreeBackend(tmp_path / "workspaces", EnvironmentConfig())

    prepared = await backend.prepare(
        "run-branch",
        RepositoryConfig(path=source, branch="horizonx/test-run"),
    )

    assert _git(prepared.path, "rev-parse", "--abbrev-ref", "HEAD") == "horizonx/test-run"
    assert _git(source, "rev-parse", "--abbrev-ref", "HEAD") == source_branch


@pytest.mark.asyncio
async def test_snapshot_preserves_source_commit_and_records_current_head(
    tmp_path: Path,
) -> None:
    source = _repository(tmp_path)
    backend = GitWorktreeBackend(tmp_path / "workspaces", EnvironmentConfig())
    prepared = await backend.prepare("run-snapshot", RepositoryConfig(path=source))
    source_commit = prepared.metadata.source_commit
    (prepared.path / "result.txt").write_text("result\n")
    _git(prepared.path, "add", "result.txt")
    _git(prepared.path, "commit", "-m", "Record result")

    snapshot = await backend.snapshot(prepared.path)

    assert snapshot.source_commit == source_commit
    assert snapshot.head_commit == _git(prepared.path, "rev-parse", "HEAD")
    assert snapshot.head_commit != snapshot.source_commit


@pytest.mark.asyncio
async def test_setup_commands_receive_only_configured_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _repository(tmp_path)
    monkeypatch.setenv("HORIZONX_ALLOWED_TEST_VALUE", "visible")
    command = (
        f"{shlex.quote(sys.executable)} -c "
        "\"import os, pathlib; "
        "pathlib.Path('setup.txt').write_text(os.environ['HORIZONX_ALLOWED_TEST_VALUE'])\""
    )
    environment = EnvironmentConfig(
        setup_commands=["mkdir -p .venv/bin", command],
        inherit_env=["HORIZONX_ALLOWED_TEST_VALUE"],
    )
    backend = GitWorktreeBackend(tmp_path / "workspaces", environment)

    prepared = await backend.prepare("run-setup", RepositoryConfig(path=source))

    assert (prepared.path / "setup.txt").read_text() == "visible"
    assert prepared.metadata.setup_complete is True
    assert prepared.env["VIRTUAL_ENV"] == str(prepared.path / ".venv")
    assert prepared.env["PATH"].split(":", maxsplit=1)[0] == str(
        prepared.path / ".venv" / "bin"
    )


@pytest.mark.asyncio
async def test_setup_failure_is_typed(tmp_path: Path) -> None:
    source = _repository(tmp_path)
    environment = EnvironmentConfig(setup_commands=["exit 17"])
    backend = GitWorktreeBackend(tmp_path / "workspaces", environment)

    with pytest.raises(SetupCommandError) as exc_info:
        await backend.prepare("run-setup-fails", RepositoryConfig(path=source))

    assert exc_info.value.result.returncode == 17


@pytest.mark.asyncio
async def test_resume_reuses_workspace_without_repeating_setup(tmp_path: Path) -> None:
    source = _repository(tmp_path)
    command = (
        f"{shlex.quote(sys.executable)} -c "
        "\"import pathlib; p=pathlib.Path('setup-count'); "
        "p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')\""
    )
    backend = GitWorktreeBackend(
        tmp_path / "workspaces", EnvironmentConfig(setup_commands=[command])
    )
    prepared = await backend.prepare("run-resume", RepositoryConfig(path=source))

    resumed = await backend.resume(prepared.path)

    assert resumed.path == prepared.path
    assert (resumed.path / "setup-count").read_text() == "1"


@pytest.mark.asyncio
async def test_resume_rejects_workspace_outside_managed_root(tmp_path: Path) -> None:
    backend = GitWorktreeBackend(tmp_path / "workspaces", EnvironmentConfig())
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(WorkspaceContainmentError):
        await backend.resume(outside)


@pytest.mark.asyncio
async def test_workspace_root_inside_source_repository_is_rejected(tmp_path: Path) -> None:
    source = _repository(tmp_path)
    backend = GitWorktreeBackend(source / "managed", EnvironmentConfig())

    with pytest.raises(WorkspaceContainmentError):
        await backend.prepare("run-overlap", RepositoryConfig(path=source))


@pytest.mark.asyncio
async def test_runtime_persists_setup_failure_as_failed_run(tmp_path: Path) -> None:
    source = _repository(tmp_path)
    task = _task(
        RepositoryConfig(path=source),
        EnvironmentConfig(setup_commands=["exit 23"]),
    )
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    try:
        with pytest.raises(SetupCommandError):
            await runtime.run(task)

        runs = await store.list_runs()
        assert runs[0]["status"] == RunStatus.FAILED.value
        assert runs[0]["completed_at"] is not None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_runtime_resume_resolves_the_original_workspace(tmp_path: Path) -> None:
    source = _repository(tmp_path)
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    task = _task(RepositoryConfig(path=source))
    try:
        run = await runtime._load_or_create(task, None)
        await runtime.prepare_workspace(run, resume=False)
        run.status = RunStatus.PAUSED_HITL
        await store.save_run(run)

        resumed = await runtime._load_or_create(task, run.id)
        await runtime.prepare_workspace(resumed, resume=True)

        assert resumed.workspace_path == run.workspace_path
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_backend_health_reports_git_availability(tmp_path: Path) -> None:
    backend = GitWorktreeBackend(tmp_path / "workspaces", EnvironmentConfig())
    health = await asyncio.wait_for(backend.health(), timeout=5)

    assert health.healthy is True
    assert health.git_version.startswith("git version")
