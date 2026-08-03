"""BaseAgent protocol — every agent driver implements this.

See docs/LONG_HORIZON_AGENT.md §24.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

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
) -> AsyncIterator[dict[str, Any]]:
    """Spawn a subprocess and yield parsed JSON events from its stdout."""
    from horizonx.security.environment_policy import (
        build_child_environment,
        redact_secrets,
    )
    from horizonx.security.process import (
        drain_stream,
        spawn_process,
        terminate_process_tree,
    )

    effective_env = build_child_environment(env)
    proc = await spawn_process(
        *cmd,
        cwd=cwd,
        env=effective_env,
        stdin=asyncio.subprocess.PIPE if stdin_data else None,
    )
    stderr_task = asyncio.create_task(
        drain_stream(proc.stderr), name=f"stderr-{proc.pid}"
    )
    if stdin_data and proc.stdin:
        proc.stdin.write(stdin_data.encode())
        await proc.stdin.drain()
        proc.stdin.close()

    assert proc.stdout is not None
    try:
        while True:
            if cancel_token and cancel_token.cancelled:
                await terminate_process_tree(proc)
                return
            if proc.returncode is not None:
                break
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=0.1)
            except TimeoutError:
                continue
            if not line:
                break
            try:
                event = json.loads(line)
                yield redact_secrets(event, effective_env)
            except json.JSONDecodeError:
                continue
        await proc.wait()
    finally:
        await terminate_process_tree(proc)
        await stderr_task
