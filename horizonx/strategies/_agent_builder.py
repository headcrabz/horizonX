"""Shared agent builder used by all strategies.

Supports built-in agents (fast path) and third-party agents installed
via horizonx.agents entry-points.
"""
from __future__ import annotations

import importlib
from importlib.metadata import entry_points
from typing import Any

_BUILTIN_AGENTS: dict[str, str] = {
    "claude_code": "horizonx.agents.claude_code:ClaudeCodeAgent",
    "codex":       "horizonx.agents.codex:CodexAgent",
    "openhands":   "horizonx.agents.openhands:OpenHandsAgent",
    "custom":      "horizonx.agents.custom:CustomAgent",
    "mock":        "horizonx.agents.mock:MockAgent",
    "sdk":         "horizonx.agents.sdk:SDKAgent",
}


def build_agent(ac: Any) -> Any:
    """Build an agent driver from AgentConfig.

    Checks built-ins first (no entry-point overhead), then falls back to
    installed horizonx.agents entry-points for third-party drivers.
    """
    # Agents that take 'config' as a keyword argument (not positional)
    _KEYWORD_CONFIG_AGENTS = frozenset({"mock"})

    if ac.type in _BUILTIN_AGENTS:
        module_path, cls_name = _BUILTIN_AGENTS[ac.type].rsplit(":", 1)
        cls = getattr(importlib.import_module(module_path), cls_name)
        if ac.type in _KEYWORD_CONFIG_AGENTS:
            return cls(config=ac)
        return cls(ac)

    eps = {ep.name: ep for ep in entry_points(group="horizonx.agents")}
    if ac.type in eps:
        cls = eps[ac.type].load()
        return cls(ac)

    raise ValueError(f"unknown agent type: {ac.type!r}")
