"""Internal control signal for one bounded runtime strategy handoff."""

from __future__ import annotations


class StrategySwitchRequested(Exception):
    """Interrupt the active strategy after its triggering attempt is durable."""

    def __init__(self, run_id: str, target: str) -> None:
        super().__init__(f"switch run {run_id} to strategy {target}")
        self.run_id = run_id
        self.target = target


class SpinControlRequested(Exception):
    """Interrupt the active strategy for a runtime-owned retry or HITL pause."""

    def __init__(self, run_id: str, action: str) -> None:
        super().__init__(f"apply {action} to run {run_id}")
        self.run_id = run_id
        self.action = action
