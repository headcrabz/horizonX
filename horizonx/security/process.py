"""Process-session ownership, bounded pipe draining, and tree termination."""

from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path
from typing import Any


async def spawn_process(
    *cmd: str,
    cwd: Path,
    env: dict[str, str],
    stdin: int | None = None,
    stream_limit_bytes: int = 64 * 1024,
) -> asyncio.subprocess.Process:
    """Spawn one command in a new OS process session when the platform supports it."""
    kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "env": env,
        "stdin": stdin,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "limit": stream_limit_bytes,
    }
    if os.name == "posix":
        kwargs["start_new_session"] = True
    return await asyncio.create_subprocess_exec(*cmd, **kwargs)


async def spawn_shell(
    command: str,
    *,
    cwd: Path,
    env: dict[str, str],
    stream_limit_bytes: int = 64 * 1024,
) -> asyncio.subprocess.Process:
    """Spawn one shell command with the same owned-session guarantees."""
    kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "env": env,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "limit": stream_limit_bytes,
    }
    if os.name == "posix":
        kwargs["start_new_session"] = True
    return await asyncio.create_subprocess_shell(command, **kwargs)


async def drain_stream(
    stream: asyncio.StreamReader | None, *, max_bytes: int = 64 * 1024
) -> bytes:
    """Drain a pipe concurrently while retaining only its bounded tail."""
    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    retained = bytearray()
    if stream is None:
        return bytes(retained)
    while chunk := await stream.read(64 * 1024):
        if max_bytes == 0:
            continue
        retained.extend(chunk)
        if len(retained) > max_bytes:
            del retained[:-max_bytes]
    return bytes(retained)


async def collect_process_output(
    process: asyncio.subprocess.Process,
    *,
    timeout: float | None = None,
    max_bytes_per_stream: int = 64 * 1024,
) -> tuple[bytes, bytes, bool]:
    """Drain both pipes concurrently with bounded retention and owned timeout cleanup."""
    stdout_task = asyncio.create_task(
        drain_stream(process.stdout, max_bytes=max_bytes_per_stream)
    )
    stderr_task = asyncio.create_task(
        drain_stream(process.stderr, max_bytes=max_bytes_per_stream)
    )
    timed_out = False
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except TimeoutError:
        timed_out = True
        await terminate_process_tree(process)
    stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
    return stdout, stderr, timed_out


async def terminate_process_tree(
    process: asyncio.subprocess.Process, *, grace_seconds: float = 2.0
) -> None:
    """Terminate the owned process session, escalating to a group kill."""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                if process.returncode is None:
                    await process.wait()
                return
            await asyncio.sleep(0.05)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.returncode is None:
            await process.wait()
        return
    elif process.returncode is not None:  # pragma: no cover
        return
    else:  # pragma: no cover - Windows CI exercises the direct fallback
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        return
    except TimeoutError:
        pass
    process.kill()  # pragma: no cover
    await process.wait()
