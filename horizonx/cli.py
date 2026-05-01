"""HorizonX CLI.

Commands: run, watch, show, list, fork, merge, export, serve.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import click
import yaml
from rich.console import Console
from rich.table import Table

from horizonx import Runtime, Task
from horizonx.storage import SqliteStore

console = Console()


@click.group()
@click.option("--db", default="horizonx.db", envvar="HORIZONX_DB", help="DB URL or path (sqlite default)")
@click.pass_context
def main(ctx: click.Context, db: str) -> None:
    """HorizonX — long-horizon agent execution harness."""
    ctx.ensure_object(dict)
    ctx.obj["db"] = db


def _load_task_from_path(path: Path) -> Task:
    if path.is_dir():
        task_yaml = path / "task.yaml"
        if not task_yaml.exists():
            raise click.ClickException(f"no task.yaml in {path}")
        path = task_yaml
    data = yaml.safe_load(path.read_text())
    return Task.model_validate(data)


@main.command()
@click.argument("task_path", type=click.Path(exists=True, path_type=Path))
@click.option("--resume", default=None, help="Resume from existing run id")
@click.option("--workspace-root", default="./horizonx-workspaces", type=Path)
@click.pass_context
def run(ctx: click.Context, task_path: Path, resume: str | None, workspace_root: Path) -> None:
    """Run a task to completion (or pause/abort)."""
    task = _load_task_from_path(task_path)
    store = SqliteStore(ctx.obj["db"])
    runtime = Runtime(store=store, workspace_root=workspace_root)
    console.print(f"[bold cyan]HorizonX[/]  starting run for task [yellow]{task.id}[/]")
    asyncio.run(runtime.run(task, resume_from=resume))


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
    rt = Runtime(store=store)
    strategy_override = json.loads(mutation) if mutation else None

    async def _fork() -> None:
        forked = await rt.fork_run(run_id, strategy_override=strategy_override)
        console.print(f"[green]Forked[/green] {run_id} → [bold]{forked.id}[/bold]")
        console.print(f"  workspace: {forked.workspace_path}")

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


if __name__ == "__main__":
    main()
