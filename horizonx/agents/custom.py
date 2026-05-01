"""CustomAgent — run any subprocess as a HorizonX agent.

Point this at any binary that can accept a task prompt and produce output.
The subprocess receives the session prompt and workspace path, and its stdout
is streamed back as Steps.

Configuration (AgentConfig.extra):
  command        str | list[str]  The executable to run (required).
  args           list[str]        Extra CLI arguments appended after command.
  prompt_mode    str              How to pass the session prompt (default: "stdin"):
                                    "stdin"   — written to the process's stdin
                                    "arg"     — appended as the last CLI argument
                                    "env"     — set as $HORIZONX_PROMPT env var
                                    "file"    — written to {workspace}/prompt.txt;
                                                path set as $HORIZONX_PROMPT_FILE
  output_format  str              How to parse stdout (default: "text"):
                                    "text"    — each non-empty line → THOUGHT step
                                    "jsonl"   — each line parsed as JSON → Step fields
                                                expected keys: type, tool_name, content
  env            dict[str, str]   Extra env vars merged into the subprocess environment.
  timeout        float            Per-session timeout in seconds (default: 1800.0).

Always-available env vars in the subprocess:
  HORIZONX_WORKSPACE  — absolute path to the session workspace directory
  HORIZONX_MODEL      — AgentConfig.model value
  HORIZONX_SESSION_ID — session identifier (may be empty on first session)

Example task.yaml:
  agent:
    type: custom
    model: my-agent-v1
    extra:
      command: /opt/my_agent/run.sh
      prompt_mode: stdin
      output_format: jsonl
      timeout: 3600
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path

from horizonx.agents.base import CancelToken, Workspace
from horizonx.core.types import AgentConfig, SessionRunResult, SessionStatus, Step, StepType

logger = logging.getLogger(__name__)

_TYPE_MAP: dict[str, StepType] = {
    "thought": StepType.THOUGHT,
    "reasoning": StepType.REASONING,
    "tool_call": StepType.TOOL_CALL,
    "observation": StepType.OBSERVATION,
    "file_change": StepType.FILE_CHANGE,
    "error": StepType.ERROR,
    "system": StepType.SYSTEM,
}


class CustomAgent:
    """Subprocess-backed agent driver for any external binary."""

    name = "custom"

    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        extra = config.extra or {}

        raw_cmd = extra.get("command")
        if raw_cmd is None:
            raise ValueError("CustomAgent requires extra.command to be set")
        if isinstance(raw_cmd, str):
            self._cmd: list[str] = shlex.split(raw_cmd)
        else:
            self._cmd = list(raw_cmd)

        self._extra_args: list[str] = [str(a) for a in extra.get("args", [])]
        self._prompt_mode: str = extra.get("prompt_mode", "stdin")
        self._output_format: str = extra.get("output_format", "text")
        self._extra_env: dict[str, str] = {str(k): str(v) for k, v in extra.get("env", {}).items()}
        self._timeout: float = float(extra.get("timeout", 1800.0))

    async def run_session(
        self,
        session_prompt: str,
        workspace: Workspace,
        *,
        resume_session_id: str | None = None,
        on_step: Callable[[Step], Awaitable[None]] | None = None,
        cancel_token: CancelToken | None = None,
        session_id: str | None = None,
    ) -> SessionRunResult:
        sid = session_id or ""

        env = {**os.environ, **workspace.env, **self._extra_env}
        env["HORIZONX_WORKSPACE"] = str(workspace.path)
        env["HORIZONX_MODEL"] = self.config.model or ""
        env["HORIZONX_SESSION_ID"] = sid

        cmd = list(self._cmd)
        stdin_data: bytes | None = None
        tmp_prompt_file: str | None = None

        if self._prompt_mode == "stdin":
            stdin_data = session_prompt.encode()
        elif self._prompt_mode == "arg":
            cmd.append(session_prompt)
        elif self._prompt_mode == "env":
            env["HORIZONX_PROMPT"] = session_prompt
        elif self._prompt_mode == "file":
            # Write to workspace so the subprocess can always find it
            prompt_path = workspace.path / "prompt.txt"
            prompt_path.write_text(session_prompt)
            env["HORIZONX_PROMPT_FILE"] = str(prompt_path)

        cmd.extend(self._extra_args)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(workspace.path),
                env=env,
                stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.error("CustomAgent binary not found: %s", cmd[0])
            return SessionRunResult(
                status=SessionStatus.ERRORED,
                error=f"custom agent binary not found: {cmd[0]!r}",
            )

        if stdin_data is not None and proc.stdin:
            proc.stdin.write(stdin_data)
            await proc.stdin.drain()
            proc.stdin.close()

        assert proc.stdout is not None
        seq = 0

        async def _drain() -> None:
            nonlocal seq
            while True:
                line = await proc.stdout.readline()  # type: ignore[union-attr]
                if not line:
                    return
                text = line.decode(errors="replace").strip()
                if not text:
                    continue
                step = self._parse_line(text, seq, sid)
                if step is not None:
                    if on_step:
                        await on_step(step)
                    seq += 1

        async def _watch_cancel(drain_task: asyncio.Task) -> None:
            while not drain_task.done():
                if cancel_token and cancel_token.cancelled:
                    drain_task.cancel()
                    return
                await asyncio.sleep(0.05)

        drain_task = asyncio.ensure_future(_drain())
        watcher = asyncio.ensure_future(_watch_cancel(drain_task))

        try:
            await asyncio.wait_for(asyncio.shield(drain_task), timeout=self._timeout)
        except asyncio.TimeoutError:
            drain_task.cancel()
            watcher.cancel()
            proc.kill()
            await asyncio.gather(drain_task, watcher, return_exceptions=True)
            return SessionRunResult(
                status=SessionStatus.TIMEOUT,
                error=f"custom agent exceeded timeout of {self._timeout}s",
            )
        except asyncio.CancelledError:
            pass
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

        if cancel_token and cancel_token.cancelled:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
            return SessionRunResult(status=SessionStatus.TIMEOUT, error=cancel_token.reason)

        await proc.wait()
        if proc.returncode == 0:
            return SessionRunResult(status=SessionStatus.COMPLETED)
        return SessionRunResult(
            status=SessionStatus.ERRORED,
            error=f"custom agent exited with code {proc.returncode}",
        )

    def _parse_line(self, text: str, seq: int, session_id: str) -> Step | None:
        if self._output_format == "jsonl":
            try:
                data = json.loads(text)
                raw_type = data.get("type", "thought")
                stype = _TYPE_MAP.get(raw_type, StepType.THOUGHT)
                return Step(
                    session_id=session_id,
                    sequence=seq,
                    type=stype,
                    tool_name=data.get("tool_name"),
                    content=data.get("content", data),
                )
            except json.JSONDecodeError:
                pass
        return Step(
            session_id=session_id,
            sequence=seq,
            type=StepType.THOUGHT,
            content={"text": text},
        )
