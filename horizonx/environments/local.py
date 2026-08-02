"""Local workspace — runs commands directly on the host filesystem.

Useful for development and trusted tasks. No isolated backend is supported yet.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from horizonx.environments.base import CommandResult


@dataclass
class LocalWorkspace:
    path: Path
    env: dict[str, str] = field(default_factory=dict)

    async def run(self, cmd: str, *, timeout: float = 60.0) -> CommandResult:
        start = time.monotonic()
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=str(self.path),
            env={**os.environ, **self.env},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            return CommandResult(returncode=-1, stdout="", stderr="timeout", elapsed=timeout)
        return CommandResult(
            returncode=proc.returncode or 0,
            stdout=(stdout or b"").decode(errors="replace"),
            stderr=(stderr or b"").decode(errors="replace"),
            elapsed=time.monotonic() - start,
        )
