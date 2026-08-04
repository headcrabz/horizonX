"""Pure normalization from recorded provider steps to canonical events."""

from __future__ import annotations

import hashlib
import json
from typing import Any, cast

from horizonx.core.types import Step
from horizonx.events.model import CanonicalEvent


def _digest(value: Any) -> str | None:
    if value is None:
        return None
    encoded = json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _category(name: str | None) -> str:
    lowered = (name or "").lower()
    if any(part in lowered for part in ("read", "glob", "grep", "list", "ls")):
        return "read"
    if any(part in lowered for part in ("search", "fetch", "web")):
        return "network" if "fetch" in lowered else "search"
    if any(part in lowered for part in ("edit", "write", "patch", "file_change")):
        return "edit"
    if any(part in lowered for part in ("bash", "command", "exec", "terminal")):
        return "execute"
    if any(part in lowered for part in ("delegate", "collab")):
        return "delegate"
    return "other"


def _canonical_tool_name(name: str | None, category: str) -> str | None:
    if category == "execute":
        return "shell"
    if category == "edit":
        return "file_edit"
    return name


def _arguments(content: dict[str, Any]) -> dict[str, Any]:
    raw = content.get("input")
    if isinstance(raw, dict):
        return raw
    return {
        key: value
        for key, value in content.items()
        if key in {"command", "path", "file_path", "old_string", "new_string", "old", "new", "changes", "query"}
    }


def normalize_step(step: Step) -> CanonicalEvent:
    """Derive stable fields without mutating the raw provider diagnostic payload."""
    content = step.content
    args = _arguments(content)
    tool_name = step.tool_name
    target = (
        args.get("file_path")
        or args.get("path")
        or args.get("command")
        or content.get("command")
    )
    result = (
        content.get("output")
        if "output" in content
        else content.get("aggregated_output", content.get("result"))
    )
    usage = cast(
        dict[str, Any], content.get("usage") if isinstance(content.get("usage"), dict) else content
    )
    changed = args.get("new_string") or args.get("new") or args.get("changes") or content.get("changes")
    error = content.get("error")
    category = _category(tool_name)
    return CanonicalEvent(
        kind=step.type.value,
        provider_kind=str(content.get("provider_kind") or step.type.value),
        tool_name=_canonical_tool_name(tool_name, category),
        category=category,  # type: ignore[arg-type]
        arguments=args,
        target=str(target) if target is not None else None,
        result_digest=_digest(result),
        exit_status=content.get("exit_code"),
        changed_file_digest=_digest(changed),
        error_classification="provider_error" if error or content.get("is_error") else None,
        provider_session_id=content.get("session_id"),
        tokens_in=usage.get("input_tokens"),
        tokens_out=usage.get("output_tokens"),
        cost_usd=content.get("total_cost_usd"),
        cumulative=content.get("usage_mode") == "cumulative",
    )
