"""Allowlisted child environments and persistence-safe secret redaction."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from horizonx.core.types import AgentConfig

_SECRET_MARKERS = (
    "API_KEY",
    "AUTH",
    "CREDENTIAL",
    "PASSWORD",
    "SECRET",
    "TOKEN",
)


def build_child_environment(environment: Mapping[str, str] | None) -> dict[str, str]:
    """Build a child environment without implicitly copying the parent process."""
    return {str(key): str(value) for key, value in (environment or {}).items()}


def _secret_values(environment: Mapping[str, str]) -> tuple[str, ...]:
    values = {
        value
        for key, value in environment.items()
        if value and any(marker in key.upper() for marker in _SECRET_MARKERS)
    }
    return tuple(sorted(values, key=len, reverse=True))


def redact_secrets(value: Any, environment: Mapping[str, str]) -> Any:
    """Recursively replace configured credential values before durable recording."""
    secrets = _secret_values(environment)

    def redact(item: Any) -> Any:
        if isinstance(item, str):
            for secret in secrets:
                item = item.replace(secret, "<redacted>")
            return item
        if isinstance(item, dict):
            return {redact(key): redact(child) for key, child in item.items()}
        if isinstance(item, list):
            return [redact(child) for child in item]
        if isinstance(item, tuple):
            return tuple(redact(child) for child in item)
        return item

    return redact(value)


def trust_boundary_metadata(
    environment: Mapping[str, str], agent: AgentConfig
) -> dict[str, Any]:
    """Describe enforced and unenforced boundaries without storing secret values."""
    permission_mode = agent.extra.get(
        "permission_mode", "default" if agent.type == "claude_code" else None
    )
    return {
        "environment": "allowlist",
        "environment_keys": sorted(environment),
        "process": "new_session",
        "workspace": "read_write",
        "network": "host_unrestricted",
        "mounts": "host_unrestricted",
        "permission_mode": permission_mode or "provider_default",
        "unsafe_permissions": permission_mode == "bypassPermissions",
        "resource_limits": {
            "wall_time": "attempt_enforced",
            "stderr_retention_bytes": 64 * 1024,
            "cpu": "not_enforced_local",
            "memory": "not_enforced_local",
            "workspace_disk": "not_enforced_local",
            "child_count": "not_enforced_local",
        },
    }
