"""HorizonX CLI.

Commands include execution, inspection, dashboard, and database maintenance.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import click
import yaml
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from horizonx import RepositoryConfig, Run, RunStatus, Runtime, Task
from horizonx.environments.base import WorkspaceError
from horizonx.project import CONFIG_FILENAME, ProjectConfig
from horizonx.storage import SqliteStore

console = Console()


@click.group()
@click.option(
    "--db",
    default=None,
    envvar="HORIZONX_DB",
    help="Path to the local SQLite database",
)
@click.pass_context
def main(ctx: click.Context, db: str | None) -> None:
    """HorizonX — long-horizon agent execution harness."""
    ctx.ensure_object(dict)
    if ctx.invoked_subcommand == "init":
        # Init must be able to replace a malformed config in the current directory.
        ctx.obj["db"] = db or Path("horizonx.db")
        ctx.obj["workspace_root"] = None
        return
    try:
        project = ProjectConfig.find_in(Path.cwd())
    except (OSError, ValidationError, yaml.YAMLError) as exc:
        raise click.ClickException(
            f"invalid {CONFIG_FILENAME}: {exc}. Fix the file or run horizonx init --force."
        ) from None
    ctx.obj["db"] = db or (project.db_path if project else Path("horizonx.db"))
    ctx.obj["workspace_root"] = project.workspace_root if project else None


@main.command()
@click.argument("directory", default=".", type=click.Path(path_type=Path))
@click.option("--force", is_flag=True, help="Overwrite existing project files")
def init(directory: Path, force: bool) -> None:
    """Create a local HorizonX project configuration and example task."""
    if directory.exists() and not directory.is_dir():
        raise click.ClickException(f"project directory is not a directory: {directory}")

    config_path = directory / CONFIG_FILENAME
    example_path = directory / "tasks" / "example.yaml"
    existing = [path for path in (config_path, example_path) if path.exists()]
    if existing and not force:
        names = ", ".join(str(path) for path in existing)
        raise click.ClickException(
            f"refusing to overwrite {names}; rerun with --force to replace them"
        )

    directory.mkdir(parents=True, exist_ok=True)
    example_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(ProjectConfig().to_yaml())
    example_path.write_text(_example_task_yaml())
    click.echo(f"Initialized HorizonX project in {directory.resolve()}")


def _example_task_yaml() -> str:
    task = {
        "id": "example",
        "name": "Example mock task",
        "description": "A local smoke test that needs no external provider.",
        "prompt": "Confirm that HorizonX can run this mock task.",
        "strategy": {"kind": "single"},
        "agent": {
            "type": "mock",
            "model": "mock",
            "extra": {
                "steps": [
                    {"type": "thought", "content": {"text": "Mock task completed."}}
                ],
                "status": "completed",
            },
        },
    }
    return yaml.safe_dump(task, sort_keys=False, default_flow_style=False)


def _load_task_from_path(path: Path) -> Task:
    if path.is_dir():
        task_yaml = path / "task.yaml"
        if not task_yaml.exists():
            raise click.ClickException(f"no task.yaml in {path}")
        path = task_yaml
    data = yaml.safe_load(path.read_text())
    task = Task.model_validate(data)
    if task.repository is not None and task.repository.path is not None:
        repository_path = task.repository.path
        if not repository_path.is_absolute():
            task.repository = task.repository.model_copy(
                update={"path": (path.parent / repository_path).resolve()}
            )
    return task


@main.command()
@click.argument("task_path", type=click.Path(exists=True, path_type=Path))
@click.option("--resume", default=None, help="Resume from existing run id")
@click.option("--workspace-root", default=None, type=Path)
@click.option("--repo", default=None, help="Local repository path or clone URL")
@click.option("--ref", "base_ref", default="HEAD", show_default=True)
@click.option("--branch", default=None, help="Create this branch in the isolated workspace")
@click.option("--submodules/--no-submodules", default=False, show_default=True)
@click.pass_context
def run(
    ctx: click.Context,
    task_path: Path,
    resume: str | None,
    workspace_root: Path | None,
    repo: str | None,
    base_ref: str,
    branch: str | None,
    submodules: bool,
) -> None:
    """Run a task to completion (or pause/abort)."""
    task = _load_task_from_path(task_path)
    if repo is not None:
        local_path = Path(repo).expanduser()
        is_url = "://" in repo or repo.startswith(("git@", "ssh:"))
        task.repository = RepositoryConfig(
            path=None if is_url else local_path.resolve(),
            url=repo if is_url else None,
            ref=base_ref,
            branch=branch,
            submodules=submodules,
        )
    elif branch is not None or base_ref != "HEAD" or submodules:
        raise click.ClickException("--ref, --branch, and --submodules require --repo")
    if workspace_root is None:
        configured_workspace = ctx.obj["workspace_root"]
        if configured_workspace is not None:
            workspace_root = configured_workspace
        elif task.repository is not None and task.repository.path is not None:
            source = task.repository.path.resolve()
            workspace_root = source.parent / f".{source.name}-horizonx-workspaces"
        else:
            workspace_root = Path("./horizonx-workspaces")
    store = SqliteStore(ctx.obj["db"])
    runtime = Runtime(store=store, workspace_root=workspace_root)
    console.print(f"[bold cyan]HorizonX[/]  starting run for task [yellow]{task.id}[/]")

    async def _run_task() -> Run:
        try:
            return await runtime.run(task, resume_from=resume)
        finally:
            await store.close()

    try:
        completed_run = asyncio.run(_run_task())
    except WorkspaceError as exc:
        raise click.ClickException(str(exc)) from None
    console.print(f"Run: [bold]{completed_run.id}[/bold]")
    console.print(f"Status: {completed_run.status.value}")
    console.print(f"Workspace: {completed_run.workspace_path}")
    if completed_run.status in {
        RunStatus.FAILED,
        RunStatus.ABORTED,
        RunStatus.TIMED_OUT,
        RunStatus.BUDGET_EXCEEDED,
    }:
        raise click.ClickException(
            f"run ended with status {completed_run.status.value}"
        )


@main.command()
@click.argument("run_id")
@click.pass_context
def show(ctx: click.Context, run_id: str) -> None:
    """Show details for a run."""
    store = SqliteStore(ctx.obj["db"])
    run_data = asyncio.run(store.load_run(run_id))
    console.print_json(run_data.model_dump_json())


@main.command(name="list")
@click.option("--limit", default=20)
@click.pass_context
def list_cmd(ctx: click.Context, limit: int) -> None:
    """List recent runs."""
    store = SqliteStore(ctx.obj["db"])
    rows = asyncio.run(store.list_runs(limit=limit))
    t = Table(title="HorizonX runs")
    t.add_column("id")
    t.add_column("status")
    t.add_column("started")
    t.add_column("completed")
    for r in rows:
        t.add_row(r["id"], r["status"], r["started_at"], r["completed_at"] or "-")
    console.print(t)


@main.command()
@click.argument("run_id")
@click.pass_context
def watch(ctx: click.Context, run_id: str) -> None:
    """Live-watch a run by tailing trajectory.jsonl."""
    store = SqliteStore(ctx.obj["db"])
    r = asyncio.run(store.load_run(run_id))
    path = Path(r.workspace_path) / "trajectory.jsonl"
    if not path.exists():
        click.echo(f"trajectory not yet at {path}; waiting...")
    import time

    pos = 0
    while True:
        if path.exists():
            with path.open("r") as f:
                f.seek(pos)
                for line in f:
                    try:
                        evt = json.loads(line)
                        click.echo(f"[{evt.get('type','?')}] {evt.get('tool_name','')} {str(evt.get('content',''))[:120]}")
                    except json.JSONDecodeError:
                        pass
                pos = f.tell()
        time.sleep(1.0)


@main.command()
@click.argument("run_id")
@click.option("--mutation", help="JSON string of strategy override, e.g. '{\"kind\":\"single\"}'")
@click.pass_context
def fork(ctx: click.Context, run_id: str, mutation: str | None) -> None:
    """Fork an existing run, optionally overriding its strategy."""
    store = SqliteStore(ctx.obj["db"])
    rt = Runtime(store=store, workspace_root=ctx.obj["workspace_root"])
    strategy_override = json.loads(mutation) if mutation else None

    async def _fork() -> None:
        try:
            forked = await rt.fork_run(run_id, strategy_override=strategy_override)
            console.print(f"[green]Forked[/green] {run_id} → [bold]{forked.id}[/bold]")
            console.print(f"  workspace: {forked.workspace_path}")
        finally:
            await store.close()

    asyncio.run(_fork())


@main.command()
@click.argument("run_id")
@click.option("--format", "fmt", default="json", type=click.Choice(["json", "yaml"]))
@click.pass_context
def export(ctx: click.Context, run_id: str, fmt: str) -> None:
    """Export a run as JSON/YAML."""
    store = SqliteStore(ctx.obj["db"])
    r = asyncio.run(store.load_run(run_id))
    data = r.model_dump(mode="json")
    if fmt == "json":
        click.echo(json.dumps(data, default=str, indent=2))
    else:
        click.echo(yaml.safe_dump(data))


@main.command()
@click.pass_context
def doctor(ctx: click.Context) -> None:
    """Check the local database schema, integrity, and connection policy."""

    async def _doctor() -> tuple[int, list[str], dict[str, int | str]]:
        store = SqliteStore(ctx.obj["db"])
        try:
            return (
                await store.schema_version(),
                await store.integrity_check(),
                await store.connection_settings(),
            )
        finally:
            await store.close()

    version, integrity, settings = asyncio.run(_doctor())
    console.print(f"Schema version: {version}")
    console.print(f"Integrity: {', '.join(integrity)}")
    console.print(
        "Connection policy: "
        f"WAL={settings['journal_mode'] == 'wal'}, "
        f"foreign keys={bool(settings['foreign_keys'])}, "
        f"busy timeout={settings['busy_timeout']}ms"
    )


@main.command()
@click.argument("destination", type=click.Path(path_type=Path))
@click.pass_context
def backup(ctx: click.Context, destination: Path) -> None:
    """Create and verify an online backup of the local database."""

    async def _backup() -> Path:
        store = SqliteStore(ctx.obj["db"])
        try:
            return await store.backup(destination)
        finally:
            await store.close()

    path = asyncio.run(_backup())
    console.print(f"Backup verified: {path}")


@main.command()
@click.argument("source", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_context
def restore(ctx: click.Context, source: Path) -> None:
    """Restore the local database from a verified backup."""

    async def _restore() -> None:
        store = SqliteStore(ctx.obj["db"])
        try:
            await store.restore(source)
        finally:
            await store.close()

    asyncio.run(_restore())
    console.print(f"Restore verified: {source}")


@main.command()
@click.pass_context
def checkpoint(ctx: click.Context) -> None:
    """Flush and truncate the local database write-ahead log."""

    async def _checkpoint() -> tuple[int, int, int]:
        store = SqliteStore(ctx.obj["db"])
        try:
            return await store.checkpoint()
        finally:
            await store.close()

    busy, log_pages, checkpointed_pages = asyncio.run(_checkpoint())
    if busy:
        raise click.ClickException("database remained busy during checkpoint")
    console.print(
        "Checkpoint complete: "
        f"log pages={log_pages}, checkpointed pages={checkpointed_pages}"
    )


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8765, type=int, show_default=True)
@click.option("--workspace-root", default=None, type=Path)
@click.pass_context
def serve(ctx: click.Context, host: str, port: int, workspace_root: Path | None) -> None:
    """Start the web dashboard (requires horizonx[dashboard])."""
    try:
        import uvicorn

        from horizonx.dashboard.app import create_app
    except ImportError as exc:
        raise click.ClickException(
            "Dashboard extras not installed. Run: pip install horizonx[dashboard]"
        ) from exc
    app = create_app(
        db_path=ctx.obj["db"],
        workspace_root=workspace_root or ctx.obj["workspace_root"] or Path("horizonx-workspaces"),
    )
    console.print(f"[bold cyan]HorizonX[/] dashboard → http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
