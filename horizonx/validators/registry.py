"""Validator registry — dispatch ValidatorConfig.type to a concrete class."""

from __future__ import annotations

from typing import Any

from horizonx.core.types import ValidatorConfig


def build_validator(vc: ValidatorConfig, *, store: Any = None):
    cfg = {**vc.config, "id": vc.id, "runs": vc.runs, "on_fail": vc.on_fail}
    if vc.type == "shell":
        from horizonx.validators.shell import ShellGate

        return ShellGate(cfg)
    if vc.type == "test_suite":
        from horizonx.validators.test_suite import TestSuiteGate

        return TestSuiteGate(cfg)
    if vc.type == "metric":
        from horizonx.validators.metric import MetricGate

        return MetricGate(cfg)
    if vc.type == "llm_judge":
        from horizonx.validators.llm_judge import LLMJudgeGate

        return LLMJudgeGate(cfg, store=store)
    if vc.type == "git":
        from horizonx.validators.git import GitGate

        return GitGate(cfg)
    if vc.type == "goal_graph":
        from horizonx.validators.goal_graph import GoalGraphGate

        return GoalGraphGate(cfg)
    raise ValueError(f"unknown validator type: {vc.type}")
