"""Provider-neutral event fields used by progress and spin analysis."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class CanonicalEvent(BaseModel):
    """A stable semantic view of one recorded provider event.

    Provider payloads remain in ``Step.content`` for diagnostics; consumers that
    make decisions use this representation instead.
    """

    kind: str
    provider_kind: str | None = None
    tool_name: str | None = None
    category: Literal["read", "search", "edit", "execute", "network", "delegate", "other"] = "other"
    arguments: dict[str, Any] = Field(default_factory=dict)
    target: str | None = None
    result_digest: str | None = None
    exit_status: int | None = None
    changed_file_digest: str | None = None
    error_classification: str | None = None
    provider_session_id: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    cumulative: bool = False
