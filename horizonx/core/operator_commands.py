"""Durable commands submitted by operators to a running attempt."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from horizonx.core.types import utcnow


def hitl_resolved_event(
    *, run_id: str, request_id: str, action: str, actor: str | None, instruction: str
) -> Any:
    from horizonx.core.event_bus import Event

    return Event(
        id=f"hitl-resolved:{request_id}",
        type="hitl.resolved",
        run_id=run_id,
        payload={
            "request_id": request_id,
            "action": action,
            "actor": actor,
            "instruction": instruction,
        },
    )


class OperatorCommandKind(str, Enum):
    CANCEL = "cancel"
    STEER = "steer"
    DECISION = "decision"


class OperatorCommand(BaseModel):
    id: str = Field(default_factory=lambda: f"command-{uuid4().hex}")
    run_id: str
    attempt_id: str | None = None
    kind: OperatorCommandKind
    actor: str
    reason: str = ""
    instruction: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str
    created_at: datetime = Field(default_factory=utcnow)
    consumed_at: datetime | None = None
