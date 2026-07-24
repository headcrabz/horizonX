"""Validator registry — dispatch ValidatorConfig.type to a concrete class."""

from __future__ import annotations

import importlib
from importlib.metadata import entry_points
from typing import Any

from horizonx.core.types import ValidatorConfig

_BUILTIN: dict[str, str] = {
    "shell":      "horizonx.validators.shell:ShellGate",
    "test_suite": "horizonx.validators.test_suite:TestSuiteGate",
    "metric":     "horizonx.validators.metric:MetricGate",
    "llm_judge":  "horizonx.validators.llm_judge:LLMJudgeGate",
    "git":        "horizonx.validators.git:GitGate",
    "goal_graph": "horizonx.validators.goal_graph:GoalGraphGate",
}


def build_validator(vc: ValidatorConfig, *, store: Any = None) -> Any:
    cfg = {**vc.config, "id": vc.id, "runs": vc.runs, "on_fail": vc.on_fail}

    # Fast path: built-ins
    if vc.type in _BUILTIN:
        module_path, cls_name = _BUILTIN[vc.type].rsplit(":", 1)
        mod = importlib.import_module(module_path)
        cls = getattr(mod, cls_name)
        # llm_judge needs store
        if vc.type == "llm_judge":
            return cls(cfg, store=store)
        return cls(cfg)

    # Plugin path: entry-points
    eps = {ep.name: ep for ep in entry_points(group="horizonx.validators")}
    if vc.type in eps:
        cls = eps[vc.type].load()
        try:
            return cls(cfg, store=store)
        except TypeError:
            return cls(cfg)

    raise ValueError(f"unknown validator type: {vc.type!r}")
