"""MetricGate — assert a metric is within range."""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from horizonx.core.types import GateAction, GateDecision, Run, Session


class MetricGate:
    name = "metric"

    def __init__(self, config: dict[str, Any]):
        self.command: str = config["command"]
        self.threshold: float = config.get("threshold", 0.0)
        self.direction: str = config.get("direction", "ge")  # ge|le|eq
        self.runs: str = config.get("runs", "after_every_session")
        self.on_fail: str = config.get("on_fail", "pause_for_hitl")
        self._id: str = config.get("id", "metric")

    async def validate(self, run: Run, session: Session | None, workspace: Any) -> GateDecision:
        start = time.monotonic()
        proc = await asyncio.create_subprocess_shell(
            self.command,
            cwd=str(workspace.path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        text = (stdout or b"").decode().strip()
        nums = re.findall(r"-?\d+\.?\d*", text)
        if not nums:
            return GateDecision(
                decision=GateAction(self.on_fail),
                reason="no numeric output from metric command",
                score=None,
                details={"output": text[-1000:]},
                validator_name=self._id,
            )
        try:
            value = float(nums[-1])
        except ValueError:
            return GateDecision(
                decision=GateAction(self.on_fail),
                reason="could not parse metric",
                score=None,
                details={"output": text[-1000:]},
                validator_name=self._id,
            )

        passed = self._passes(value)
        return GateDecision(
            decision=GateAction.CONTINUE if passed else GateAction(self.on_fail),
            reason=f"metric={value} {self.direction} {self.threshold}",
            score=value,
            details={"value": value, "threshold": self.threshold, "direction": self.direction},
            validator_name=self._id,
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    def _passes(self, value: float) -> bool:
        if self.direction == "ge":
            return value >= self.threshold
        if self.direction == "le":
            return value <= self.threshold
        return abs(value - self.threshold) < 1e-9
