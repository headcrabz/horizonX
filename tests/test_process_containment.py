"""Process-tree and output-pressure contracts for agent subprocesses."""

from __future__ import annotations

import asyncio
import os
import shlex
import signal
import sys
from pathlib import Path

import pytest

from horizonx.agents.base import CancelToken, stream_subprocess_jsonl
from horizonx.core.attempt_executor import AttemptExecutor
from horizonx.core.runtime import Runtime
from horizonx.core.types import (
    AgentConfig,
    Run,
    RunStatus,
    SessionStatus,
    StrategyConfig,
    Task,
)
from horizonx.environments.local import LocalWorkspace
from horizonx.storage.sqlite import SqliteStore


@pytest.mark.asyncio
async def test_cancellation_terminates_spawned_grandchild(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    ready_file = tmp_path / "grandchild.ready"
    stopped_file = tmp_path / "grandchild.stopped"
    grandchild_script = "\n".join(
        [
            "import pathlib, signal, sys, time",
            f"ready = pathlib.Path({str(ready_file)!r})",
            f"stopped = pathlib.Path({str(stopped_file)!r})",
            "def stop(*args):",
            "    stopped.write_text('terminated')",
            "    sys.exit(0)",
            "signal.signal(signal.SIGTERM, stop)",
            "ready.write_text('ready')",
            "time.sleep(60)",
        ]
    )
    script = "\n".join(
        [
            "import json, pathlib, subprocess, sys, time",
            f"child = subprocess.Popen([sys.executable, '-c', {grandchild_script!r}])",
            f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))",
            f"ready = pathlib.Path({str(ready_file)!r})",
            "while not ready.exists(): time.sleep(0.01)",
            "print(json.dumps({'type': 'ready'}), flush=True)",
            "time.sleep(60)",
        ]
    )
    token = CancelToken()

    async def consume() -> None:
        async for event in stream_subprocess_jsonl(
            [sys.executable, "-c", script],
            cwd=tmp_path,
            env={"PATH": os.environ.get("PATH", "")},
            cancel_token=token,
        ):
            assert event["type"] == "ready"
            token.cancel("test cancellation")

    await asyncio.wait_for(consume(), timeout=5)
    grandchild_pid = int(pid_file.read_text())
    try:
        for _ in range(40):
            if stopped_file.is_file():
                break
            await asyncio.sleep(0.05)
        assert stopped_file.read_text() == "terminated"
    finally:
        try:
            os.kill(grandchild_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.mark.asyncio
async def test_cleanup_terminates_background_child_after_parent_exit(
    tmp_path: Path,
) -> None:
    stopped_file = tmp_path / "background.stopped"
    ready_file = tmp_path / "background.ready"
    child_script = "\n".join(
        [
            "import pathlib, signal, sys, time",
            f"ready = pathlib.Path({str(ready_file)!r})",
            f"stopped = pathlib.Path({str(stopped_file)!r})",
            "def stop(*args):",
            "    stopped.write_text('terminated')",
            "    sys.exit(0)",
            "signal.signal(signal.SIGTERM, stop)",
            "ready.write_text('ready')",
            "time.sleep(60)",
        ]
    )
    parent_script = "\n".join(
        [
            "import json, pathlib, subprocess, sys, time",
            f"subprocess.Popen([sys.executable, '-c', {child_script!r}])",
            f"ready = pathlib.Path({str(ready_file)!r})",
            "while not ready.exists(): time.sleep(0.01)",
            "print(json.dumps({'type': 'ready'}), flush=True)",
        ]
    )

    events = [
        event
        async for event in stream_subprocess_jsonl(
            [sys.executable, "-c", parent_script], cwd=tmp_path, env={}
        )
    ]

    assert events == [{"type": "ready"}]
    assert stopped_file.read_text() == "terminated"


@pytest.mark.asyncio
async def test_large_stderr_is_drained_without_deadlock(tmp_path: Path) -> None:
    script = (
        "import json,sys; "
        "sys.stderr.write('x' * 2_000_000); sys.stderr.flush(); "
        "print(json.dumps({'type':'completed'}), flush=True)"
    )

    async def consume() -> list[dict[str, object]]:
        return [
            event
            async for event in stream_subprocess_jsonl(
                [sys.executable, "-c", script], cwd=tmp_path, env={}
            )
        ]

    events = await asyncio.wait_for(consume(), timeout=5)
    assert events == [{"type": "completed"}]


@pytest.mark.asyncio
async def test_attempt_timeout_cancels_custom_subprocess(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store, workspace_root=tmp_path / "workspaces")
    run = Run(
        id="run-custom-timeout",
        task=Task(
            id="custom-timeout",
            name="Custom timeout",
            prompt="Stop the child",
            strategy=StrategyConfig(kind="single"),
            agent=AgentConfig(
                type="custom",
                model="custom",
                extra={
                    "command": [
                        sys.executable,
                        "-c",
                        "import time; time.sleep(60)",
                    ]
                },
            ),
        ),
        workspace_path=tmp_path / "workspace",
        status=RunStatus.RUNNING,
    )
    run.workspace_path.mkdir()
    await store.save_run(run)
    try:
        result = await asyncio.wait_for(
            AttemptExecutor(runtime).execute(
                run, prompt="wait", timeout_seconds=0.1
            ),
            timeout=5,
        )

        assert result.status == SessionStatus.TIMEOUT
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_local_command_retains_only_bounded_output_tail(tmp_path: Path) -> None:
    script = "import sys; sys.stdout.write('x' * 1_000_000 + 'TAIL')"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    result = await LocalWorkspace(tmp_path, {}).run(command)

    assert result.returncode == 0
    assert len(result.stdout.encode()) <= 64 * 1024
    assert result.stdout.endswith("TAIL")
