"""Typed result of one bounded agent attempt."""

from __future__ import annotations

from pydantic import BaseModel, Field

from horizonx.core.types import (
    AttemptRecord,
    GateDecision,
    Session,
    SessionRunResult,
    SessionStatus,
)


class AttemptResult(BaseModel):
    attempt: AttemptRecord
    session: Session
    agent: SessionRunResult
    decisions: list[GateDecision] = Field(default_factory=list)
    spin_detected: bool = False

    @property
    def status(self) -> SessionStatus:
        return self.agent.status

    @property
    def succeeded(self) -> bool:
        return self.status == SessionStatus.COMPLETED
