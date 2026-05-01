"""ShellGate — generic shell command exit-code-as-gate."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from horizonx.core.types import GateAction, GateDecision, Run, Session


class ShellGate:
    name = "shell"

    def __init__(self, config: dict[str, Any]):
        self.command: str = config["command"]
        self.timeout_seconds: float = config.get("timeout_seconds", 60.0)
        self.on_fail: str = config.get("on_fail", "pause_for_hitl")
        self.runs: str = config.get("runs", "after_every_session")
        self._id: str = config.get("id", "shell")

    async def validate(self, run: Run, session: Session | None, workspace: Any) -> GateDecision:
        start = time.monotonic()
        proc = await asyncio.create_subprocess_shell(
            self.command,
            cwd=str(workspace.path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout_seconds
            )
        except asyncio.TimeoutError:
            proc.kill()
            return GateDecision(
                decision=GateAction.PAUSE_FOR_HITL,
                reason=f"timeout after {self.timeout_seconds}s",
                score=0.0,
                details={"command": self.command},
                validator_name=self._id,
                duration_ms=int((time.monotonic() - start) * 1000),
            )

        passed = proc.returncode == 0
        decision_action = (
            GateAction.CONTINUE
            if passed
            else GateAction(self.on_fail) if self.on_fail in {a.value for a in GateAction} else GateAction.PAUSE_FOR_HITL
        )
        return GateDecision(
            decision=decision_action,
            reason="exit 0" if passed else f"exit {proc.returncode}",
            score=1.0 if passed else 0.0,
            details={
                "command": self.command,
                "returncode": proc.returncode,
                "stdout_tail": (stdout or b"").decode(errors="replace")[-2000:],
                "stderr_tail": (stderr or b"").decode(errors="replace")[-2000:],
            },
            validator_name=self._id,
            duration_ms=int((time.monotonic() - start) * 1000),
        )
