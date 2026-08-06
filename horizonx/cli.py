"""HorizonX CLI.

Commands include execution, inspection, dashboard, and database maintenance.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import click
import yaml
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from horizonx import RepositoryConfig, Run, RunStatus, Runtime, Task
from horizonx.core.operator_commands import OperatorCommand, OperatorCommandKind
from horizonx.core.types import TERMINAL_RUN_STATUSES
from horizonx.environments.base import WorkspaceError
from horizonx.project import CONFIG_FILENAME, ProjectConfig
from horizonx.storage import SqliteStore
from horizonx.storage.sqlite import StoreError

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


async def _load_run_context(store: SqliteStore, run_id: str) -> tuple[Run, str | None, str | None]:
    """Load a run and its most specific durable provider-session identity."""
    run = await store.load_run(run_id)
    attempts = await store.list_attempts(run_id)
    if attempts:
        latest = attempts[-1]
        recorded = next(
            (attempt for attempt in reversed(attempts) if attempt.provider_session_id), None
        )
        if recorded is not None:
            return run, recorded.provider, recorded.provider_session_id
        return run, latest.provider, None
    sessions = await store.list_sessions(run_id)
    if sessions:
        return run, run.task.agent.type, sessions[-1].agent_session_id
    return run, run.task.agent.type, None


def _cost_text(run: Run) -> str:
    if not run.cumulative.cost_known or run.cumulative.usd is None:
        return "unknown"
    return f"${run.cumulative.usd:.2f}"


def _provider_session_text(provider: str | None, session_id: str | None) -> str:
    if not session_id:
        return "not recorded"
    return f"{provider or 'provider'} {session_id}"


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

    async def _run_task() -> tuple[Run, str | None, str | None]:
        try:
            completed = await runtime.run(task, resume_from=resume)
            _, provider, session_id = await _load_run_context(store, completed.id)
            return completed, provider, session_id
        finally:
            if hasattr(runtime, "shutdown"):
                await runtime.shutdown(close_store=False)
            await store.close()

    try:
        completed_run, provider, session_id = asyncio.run(_run_task())
    except WorkspaceError as exc:
        raise click.ClickException(str(exc)) from None
    console.print(f"Run: [bold]{completed_run.id}[/bold]")
    console.print(f"Status: {completed_run.status.value}")
    console.print(f"Workspace: {completed_run.workspace_path}")
    console.print(f"Provider session: {_provider_session_text(provider, session_id)}")
    console.print(f"Cost: {_cost_text(completed_run)}")
    console.print(f"Attach: horizonx attach {completed_run.id}")
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
    async def _show() -> Run:
        store = SqliteStore(ctx.obj["db"])
        try:
            return await store.load_run(run_id)
        finally:
            await store.close()

    try:
        run_data = asyncio.run(_show())
    except KeyError:
        raise click.ClickException(f"run not found: {run_id}") from None
    console.print_json(run_data.model_dump_json())


@main.command()
@click.argument("run_id")
@click.pass_context
def status(ctx: click.Context, run_id: str) -> None:
    """Show a compact durable summary of a run."""

    async def _status() -> tuple[Run, str | None, str | None]:
        store = SqliteStore(ctx.obj["db"])
        try:
            return await _load_run_context(store, run_id)
        finally:
            await store.close()

    try:
        run, provider, session_id = asyncio.run(_status())
    except KeyError:
        raise click.ClickException(f"run not found: {run_id}") from None
    click.echo(f"Status: {run.status.value}")
    click.echo(f"Workspace: {run.workspace_path}")
    click.echo(f"Provider session: {_provider_session_text(provider, session_id)}")
    click.echo(f"Cost: {_cost_text(run)}")


@main.command()
@click.argument("run_id")
@click.pass_context
def attach(ctx: click.Context, run_id: str) -> None:
    """Print durable context and a provider resume hint, when available."""

    async def _attach() -> tuple[Run, str | None, str | None]:
        store = SqliteStore(ctx.obj["db"])
        try:
            return await _load_run_context(store, run_id)
        finally:
            await store.close()

    try:
        run, provider, session_id = asyncio.run(_attach())
    except KeyError:
        raise click.ClickException(f"run not found: {run_id}") from None
    click.echo(f"Workspace: {run.workspace_path}")
    if not session_id:
        click.echo("No provider session is recorded; cannot reattach a lost process.")
        return
    click.echo(f"Provider session: {_provider_session_text(provider, session_id)}")
    normalized = (provider or "").lower()
    if normalized in {"claude", "claude_code"}:
        click.echo(f"Resume hint: cd {run.workspace_path} && claude --resume {session_id}")
    elif normalized == "codex":
        click.echo(f"Resume hint: cd {run.workspace_path} && codex exec resume {session_id}")
    else:
        click.echo("No provider-specific resume hint is available; cannot reattach a lost process.")


@main.command()
@click.argument("run_id")
@click.option("--format", "fmt", default="json", type=click.Choice(["json", "yaml"]))
@click.pass_context
def evidence(ctx: click.Context, run_id: str, fmt: str) -> None:
    """Export durable evidence recorded for a run."""

    async def _evidence() -> dict[str, object]:
        store = SqliteStore(ctx.obj["db"])
        try:
            run = await store.load_run(run_id)
            goals, validations, sessions, attempts, spin_reports, events = await asyncio.gather(
                store.list_goals(run_id), store.list_validations(run_id),
                store.list_sessions(run_id), store.list_attempts(run_id),
                store.list_spin_reports(run_id), store.list_events(run_id),
            )
            return {
                "run": run.model_dump(mode="json"),
                "goals": [item.model_dump(mode="json") for item in goals],
                "validations": validations,
                "sessions": [item.model_dump(mode="json") for item in sessions],
                "attempts": [item.model_dump(mode="json") for item in attempts],
                "spins": spin_reports,
                "events": [item.model_dump(mode="json") for item in events],
            }
        finally:
            await store.close()

    try:
        bundle = asyncio.run(_evidence())
    except KeyError:
        raise click.ClickException(f"run not found: {run_id}") from None
    if fmt == "json":
        click.echo(json.dumps(bundle, indent=2, default=str))
    else:
        click.echo(yaml.safe_dump(bundle, sort_keys=False))


async def _submit_operator_command(
    store: SqliteStore, command: OperatorCommand, *, cancel: bool = False
) -> bool:
    if cancel:
        _, created, _ = await store.submit_cancel_command(command)
    else:
        _, created = await store.submit_steer_command(command)
    return created


@main.command()
@click.argument("run_id")
@click.argument("instruction")
@click.option("--reason", default="")
@click.option("--idempotency-key", default=None)
@click.pass_context
def steer(
    ctx: click.Context, run_id: str, instruction: str, reason: str, idempotency_key: str | None
) -> None:
    """Durably submit a steering instruction for a nonterminal run."""

    async def _steer() -> bool:
        store = SqliteStore(ctx.obj["db"])
        try:
            return await _submit_operator_command(
                store,
                OperatorCommand(
                    run_id=run_id, kind=OperatorCommandKind.STEER, actor="cli",
                    reason=reason, instruction=instruction,
                    idempotency_key=idempotency_key or f"steer-{uuid4().hex}",
                ),
            )
        finally:
            await store.close()

    try:
        created = asyncio.run(_steer())
    except (KeyError, StoreError) as exc:
        raise click.ClickException(str(exc)) from None
    click.echo("Steer command accepted." if created else "Steer command duplicate.")


@main.command()
@click.argument("run_id")
@click.option("--reason", default="")
@click.option("--idempotency-key", default=None)
@click.pass_context
def cancel(ctx: click.Context, run_id: str, reason: str, idempotency_key: str | None) -> None:
    """Durably submit and apply a cancellation command."""

    async def _cancel() -> bool:
        store = SqliteStore(ctx.obj["db"])
        try:
            return await _submit_operator_command(
                store,
                OperatorCommand(
                    run_id=run_id, kind=OperatorCommandKind.CANCEL, actor="cli",
                    reason=reason, idempotency_key=idempotency_key or f"cancel-{run_id}",
                ),
                cancel=True,
            )
        finally:
            await store.close()

    try:
        created = asyncio.run(_cancel())
    except (KeyError, StoreError) as exc:
        raise click.ClickException(str(exc)) from None
    click.echo("Cancel command accepted." if created else "Cancel command duplicate.")


@main.command()
@click.argument("run_id")
@click.pass_context
def resume(ctx: click.Context, run_id: str) -> None:
    """Resume execution from a durable task snapshot; no process is reattached."""
    store = SqliteStore(ctx.obj["db"])
    runtime = Runtime(
        store=store,
        workspace_root=ctx.obj["workspace_root"] or Path("horizonx-workspaces"),
    )

    async def _resume() -> Run:
        try:
            snapshot = await store.load_run(run_id)
            if snapshot.status in TERMINAL_RUN_STATUSES:
                raise StoreError(f"run is already terminal: {snapshot.status.value}")
            return await runtime.run(snapshot.task, resume_from=run_id)
        finally:
            if hasattr(runtime, "shutdown"):
                await runtime.shutdown(close_store=False)
            await store.close()

    try:
        resumed = asyncio.run(_resume())
    except (KeyError, StoreError) as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(f"Run: {resumed.id}")
    click.echo(f"Status: {resumed.status.value}")
    click.echo("Resumed from durable snapshot; no lost process was reattached.")


@main.command(name="list")
@click.option("--limit", default=20)
@click.pass_context
def list_cmd(ctx: click.Context, limit: int) -> None:
    """List recent runs."""
    async def _list() -> list[dict[str, object]]:
        store = SqliteStore(ctx.obj["db"])
        try:
            return await store.list_runs(limit=limit)
        finally:
            await store.close()

    rows = asyncio.run(_list())
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
@click.option("--interval", default=0.1, type=float, show_default=True)
@click.pass_context
def watch(ctx: click.Context, run_id: str, interval: float) -> None:
    """Watch durable events until the run reaches a terminal status."""

    async def _poll() -> None:
        store = SqliteStore(ctx.obj["db"])
        cursor: int | None = None
        try:
            while True:
                run = await store.load_run(run_id)
                events = await store.list_events(run_id, after_sequence=cursor)
                for event in events:
                    cursor = event.sequence
                    click.echo(f"[{event.type}] {json.dumps(event.payload, default=str)}")
                if run.status in TERMINAL_RUN_STATUSES:
                    click.echo(f"Status: {run.status.value}")
                    return
                await asyncio.sleep(interval)
        finally:
            await store.close()

    if interval < 0:
        raise click.BadParameter("must be non-negative", param_hint="--interval")
    try:
        asyncio.run(_poll())
    except KeyError:
        raise click.ClickException(f"run not found: {run_id}") from None


@main.command()
@click.argument("run_id")
@click.option("--mutation", help="JSON string of strategy override, e.g. '{\"kind\":\"single\"}'")
@click.pass_context
def fork(ctx: click.Context, run_id: str, mutation: str | None) -> None:
    """Fork an existing run, optionally overriding its strategy."""
    try:
        strategy_override = json.loads(mutation) if mutation else None
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"invalid --mutation JSON: {exc.msg}") from None
    store = SqliteStore(ctx.obj["db"])
    rt = Runtime(
        store=store,
        workspace_root=ctx.obj["workspace_root"] or Path("horizonx-workspaces"),
    )

    async def _fork() -> None:
        try:
            forked = await rt.fork_run(run_id, strategy_override=strategy_override)
            console.print(f"[green]Forked[/green] {run_id} → [bold]{forked.id}[/bold]")
            console.print(f"  workspace: {forked.workspace_path}")
        finally:
            if hasattr(rt, "shutdown"):
                await rt.shutdown(close_store=False)
            await store.close()

    asyncio.run(_fork())


@main.command()
@click.argument("run_id")
@click.option("--format", "fmt", default="json", type=click.Choice(["json", "yaml"]))
@click.pass_context
def export(ctx: click.Context, run_id: str, fmt: str) -> None:
    """Export a run as JSON/YAML."""
    async def _export() -> Run:
        store = SqliteStore(ctx.obj["db"])
        try:
            return await store.load_run(run_id)
        finally:
            await store.close()

    try:
        r = asyncio.run(_export())
    except KeyError:
        raise click.ClickException(f"run not found: {run_id}") from None
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
