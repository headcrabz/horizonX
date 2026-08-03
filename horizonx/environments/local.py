"""Local workspace — runs commands directly on the host filesystem.

Useful for development and trusted tasks. No isolated backend is supported yet.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from horizonx.environments.base import CommandResult
from horizonx.security.environment_policy import build_child_environment, redact_secrets
from horizonx.security.process import collect_process_output, spawn_shell


@dataclass
class LocalWorkspace:
    path: Path
    env: dict[str, str] = field(default_factory=dict)

    async def run(self, cmd: str, *, timeout: float = 60.0) -> CommandResult:
        start = time.monotonic()
        proc = await spawn_shell(
            cmd, cwd=self.path, env=build_child_environment(self.env)
        )
        stdout, stderr, timed_out = await collect_process_output(
            proc, timeout=timeout
        )
        if timed_out:
            return CommandResult(returncode=-1, stdout="", stderr="timeout", elapsed=timeout)
        return CommandResult(
            returncode=proc.returncode or 0,
            stdout=str(
                redact_secrets(
                    (stdout or b"").decode(errors="replace"), self.env
                )
            ),
            stderr=str(
                redact_secrets(
                    (stderr or b"").decode(errors="replace"), self.env
                )
            ),
            elapsed=time.monotonic() - start,
        )
