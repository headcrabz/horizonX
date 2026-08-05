"""Built-in and entry-point agent resolution."""

from __future__ import annotations

import importlib
from importlib.metadata import entry_points
from typing import Any, cast

from horizonx.agents.base import BaseAgent

_BUILTIN_AGENTS: dict[str, str] = {
    "claude_code": "horizonx.agents.claude_code:ClaudeCodeAgent",
    "codex": "horizonx.agents.codex:CodexAgent",
    "openhands": "horizonx.agents.openhands:OpenHandsAgent",
    "custom": "horizonx.agents.custom:CustomAgent",
    "mock": "horizonx.agents.mock:MockAgent",
    "sdk": "horizonx.agents.sdk:SDKAgent",
}


def build_agent(config: Any) -> BaseAgent:
    """Resolve an agent without making strategies depend on concrete drivers."""
    if config.type in _BUILTIN_AGENTS:
        module_path, class_name = _BUILTIN_AGENTS[config.type].rsplit(":", 1)
        agent_class = getattr(importlib.import_module(module_path), class_name)
        if config.type == "mock":
            return cast(BaseAgent, agent_class(config=config))
        return cast(BaseAgent, agent_class(config))

    installed = {entry.name: entry for entry in entry_points(group="horizonx.agents")}
    if config.type in installed:
        return cast(BaseAgent, installed[config.type].load()(config))

    available = sorted({*_BUILTIN_AGENTS, *installed})
    raise ValueError(
        f"unknown agent type {config.type!r}; available agents: {', '.join(available)}"
    )
