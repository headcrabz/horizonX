"""BaseAgent protocol — every agent driver implements this.

See docs/LONG_HORIZON_AGENT.md §24.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from horizonx.core.types import SessionRunResult, Step


@dataclass
class CancelToken:
    """Cooperative cancellation. Strategies set .cancelled = True; agents check it."""

    cancelled: bool = False
    reason: str = ""

    def cancel(self, reason: str = "") -> None:
        self.cancelled = True
        self.reason = reason


@dataclass
class Workspace:
    """Filesystem context for an agent session."""

    path: Path
    env: dict[str, str]


class BaseAgent(Protocol):
    """Driver protocol. Implement run_session and you get all observability free."""

    name: str

    async def run_session(
        self,
        session_prompt: str,
        workspace: Workspace,
        *,
        resume_session_id: str | None = None,
        on_step: Callable[[Step], Awaitable[None]] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> SessionRunResult:
        """Run one bounded agent session.

        Yield events to on_step in real-time. Honor cancel_token. Return final
        agent_session_id (for Claude Code / Codex resume) and status.
        """
        ...


# ---------------------------------------------------------------------------
# Subprocess streaming helper — used by Claude Code + Codex drivers
# ---------------------------------------------------------------------------


async def stream_subprocess_jsonl(
    cmd: list[str],
    cwd: Path,
    stdin_data: str | None = None,
    env: dict[str, str] | None = None,
    cancel_token: CancelToken | None = None,
):
    """Spawn a subprocess and yield parsed JSON events from its stdout."""
    import json

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        env=env,
        stdin=asyncio.subprocess.PIPE if stdin_data else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    if stdin_data and proc.stdin:
        proc.stdin.write(stdin_data.encode())
        await proc.stdin.drain()
        proc.stdin.close()

    assert proc.stdout is not None
    while True:
        if cancel_token and cancel_token.cancelled:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
            return
        line = await proc.stdout.readline()
        if not line:
            break
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            # Skip non-JSON lines (warnings, etc.)
            continue
    await proc.wait()
    return
