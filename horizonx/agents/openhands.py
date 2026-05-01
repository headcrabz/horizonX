"""OpenHands agent driver.

Wraps the OpenHands CLI (openhands run / oh run) to execute agent sessions.
OpenHands exposes a REST API when run as a server, or a one-shot CLI for
single-session execution.

This driver supports:
  - One-shot CLI mode (default): `oh run --task "..." --workspace ...`
  - Server mode: POST /api/conversations to start, GET events to stream

Configuration (AgentConfig.extra):
  mode:           "cli" | "server" (default: "cli")
  server_url:     base URL for server mode (default: "http://localhost:3000")
  cli_bin:        CLI binary name or path (default: "openhands")
  runtime:        OpenHands runtime to use (e.g. "docker", "local")
  agent_cls:      OpenHands agent class (default: "CodeActAgent")
  max_iterations: max agent iterations per session (default: 30)
  headless:       run headless (default: True)

See docs/LONG_HORIZON_AGENT.md §24.3.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

from horizonx.agents.base import CancelToken, Workspace
from horizonx.core.types import AgentConfig, SessionRunResult, SessionStatus, Step, StepType

logger = logging.getLogger(__name__)


class OpenHandsAgent:
    name = "openhands"

    def __init__(self, config: AgentConfig):
        self.config = config
        extra = config.extra or {}
        self.mode: str = extra.get("mode", "cli")
        self.server_url: str = extra.get("server_url", "http://localhost:3000")
        self.cli_bin: str = extra.get("cli_bin", "openhands")
        self.runtime: str | None = extra.get("runtime")
        self.agent_cls: str = extra.get("agent_cls", "CodeActAgent")
        self.max_iterations: int = extra.get("max_iterations", 30)

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
        if self.mode == "server":
            return await self._run_server(
                session_prompt, workspace, on_step=on_step,
                cancel_token=cancel_token, session_id=session_id,
            )
        return await self._run_cli(
            session_prompt, workspace, on_step=on_step,
            cancel_token=cancel_token, session_id=session_id,
        )

    async def _run_cli(
        self,
        session_prompt: str,
        workspace: Workspace,
        *,
        on_step: Callable[[Step], Awaitable[None]] | None = None,
        cancel_token: CancelToken | None = None,
        session_id: str | None = None,
    ) -> SessionRunResult:
        cmd = [
            self.cli_bin,
            "--task", session_prompt,
            "--workspace-dir", str(workspace.path),
            "--agent-cls", self.agent_cls,
            "--max-iterations", str(self.max_iterations),
            "--headless",
        ]
        if self.config.model:
            cmd += ["--model", self.config.model]
        if self.runtime:
            cmd += ["--runtime", self.runtime]

        env = {**os.environ, **workspace.env}

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(workspace.path),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.warning("OpenHands CLI binary %r not found", self.cli_bin)
            return SessionRunResult(
                status=SessionStatus.ERRORED,
                error=f"OpenHands CLI binary not found: {self.cli_bin}",
            )

        seq = 0
        assert proc.stdout is not None
        while True:
            if cancel_token and cancel_token.cancelled:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    proc.kill()
                return SessionRunResult(status=SessionStatus.TIMEOUT)

            line = await proc.stdout.readline()
            if not line:
                break

            text = line.decode(errors="replace").strip()
            if not text:
                continue

            step = self._parse_cli_line(text, seq, session_id or "")
            if step and on_step:
                await on_step(step)
                seq += 1

        await proc.wait()
        if proc.returncode == 0:
            return SessionRunResult(status=SessionStatus.COMPLETED)
        return SessionRunResult(
            status=SessionStatus.ERRORED,
            error=f"openhands exited with code {proc.returncode}",
        )

    async def _run_server(
        self,
        session_prompt: str,
        workspace: Workspace,
        *,
        on_step: Callable[[Step], Awaitable[None]] | None = None,
        cancel_token: CancelToken | None = None,
        session_id: str | None = None,
    ) -> SessionRunResult:
        try:
            import httpx
        except ImportError:
            return SessionRunResult(
                status=SessionStatus.ERRORED,
                error="httpx not installed — required for OpenHands server mode",
            )

        async with httpx.AsyncClient(base_url=self.server_url, timeout=30.0) as client:
            # Start conversation
            try:
                resp = await client.post("/api/conversations", json={
                    "task": session_prompt,
                    "agent_cls": self.agent_cls,
                    "max_iterations": self.max_iterations,
                    "selected_repository": str(workspace.path),
                })
                resp.raise_for_status()
                conv_id = resp.json().get("conversation_id") or resp.json().get("id")
            except Exception as exc:
                return SessionRunResult(
                    status=SessionStatus.ERRORED, error=f"failed to start OpenHands conversation: {exc}",
                )

            # Poll events
            seq = 0
            poll_interval = 2.0
            max_wait = self.max_iterations * 30.0
            start = time.monotonic()

            while (time.monotonic() - start) < max_wait:
                if cancel_token and cancel_token.cancelled:
                    return SessionRunResult(status=SessionStatus.TIMEOUT)

                try:
                    events_resp = await client.get(f"/api/conversations/{conv_id}/events")
                    events_resp.raise_for_status()
                    events = events_resp.json()
                except Exception:
                    await asyncio.sleep(poll_interval)
                    continue

                for event in events:
                    step = self._parse_server_event(event, seq, session_id or "")
                    if step and on_step:
                        await on_step(step)
                        seq += 1

                    if event.get("type") in ("agent_state_changed",) and event.get("extras", {}).get("agent_state") in ("finished", "error"):
                        finished = event.get("extras", {}).get("agent_state") == "finished"
                        return SessionRunResult(
                            agent_session_id=str(conv_id),
                            status=SessionStatus.COMPLETED if finished else SessionStatus.ERRORED,
                        )

                await asyncio.sleep(poll_interval)

        return SessionRunResult(
            agent_session_id=str(conv_id),
            status=SessionStatus.TIMEOUT,
        )

    def _parse_cli_line(self, text: str, seq: int, session_id: str) -> Step | None:
        try:
            data = json.loads(text)
            stype = StepType.THOUGHT
            if data.get("type") == "action":
                stype = StepType.TOOL_CALL
            elif data.get("type") == "observation":
                stype = StepType.OBSERVATION
            return Step(
                session_id=session_id,
                sequence=seq,
                type=stype,
                content=data,
            )
        except json.JSONDecodeError:
            return Step(
                session_id=session_id,
                sequence=seq,
                type=StepType.THOUGHT,
                content={"text": text},
            )

    def _parse_server_event(self, event: dict[str, Any], seq: int, session_id: str) -> Step | None:
        etype = event.get("type", "")
        if etype in ("agent_state_changed", "status_update"):
            return None
        stype = StepType.OBSERVATION if etype == "observation" else StepType.TOOL_CALL if etype == "action" else StepType.THOUGHT
        return Step(
            session_id=session_id,
            sequence=seq,
            type=stype,
            tool_name=event.get("action") or event.get("observation"),
            content=event,
        )
