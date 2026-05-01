"""BaseValidator protocol. See docs/LONG_HORIZON_AGENT.md §25."""

from __future__ import annotations

from typing import Any, Protocol

from horizonx.core.types import GateDecision, Run, Session


class BaseValidator(Protocol):
    name: str
    runs: str  # "after_every_session" | "every_n_sessions" | etc.

    async def validate(
        self, run: Run, session: Session | None, workspace: Any
    ) -> GateDecision: ...
