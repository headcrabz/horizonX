"""Authentication and parsing helpers for Slack interactive callbacks."""

from __future__ import annotations

import hashlib
import hmac
import time


def verify_slack_signature(
    *,
    body: bytes,
    timestamp: str,
    signature: str,
    signing_secret: str,
    now: int | None = None,
    tolerance_seconds: int = 300,
) -> None:
    """Verify Slack's v0 HMAC and reject replayed, stale requests."""
    try:
        request_time = int(timestamp)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid Slack timestamp") from exc
    current = int(time.time()) if now is None else now
    if abs(current - request_time) > tolerance_seconds:
        raise ValueError("stale Slack request")
    base = f"v0:{timestamp}:".encode() + body
    expected = "v0=" + hmac.new(
        signing_secret.encode(), base, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise ValueError("invalid Slack signature")
