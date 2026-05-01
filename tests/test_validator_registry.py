"""Tests for horizonx.validators.registry."""

from __future__ import annotations

import pytest


class TestValidatorRegistryWiring:
    def test_git_gate_registered(self):
        from horizonx.validators.registry import build_validator
        from horizonx.core.types import ValidatorConfig
        from horizonx.validators.git import GitGate
        vc = ValidatorConfig(id="mygit", type="git", config={"min_commits": 1})
        gate = build_validator(vc)
        assert isinstance(gate, GitGate)
        assert gate.min_commits == 1

    def test_goal_graph_gate_registered(self):
        from horizonx.validators.registry import build_validator
        from horizonx.core.types import ValidatorConfig
        from horizonx.validators.goal_graph import GoalGraphGate
        vc = ValidatorConfig(id="mygg", type="goal_graph", config={"min_completion_pct": 0.5})
        gate = build_validator(vc)
        assert isinstance(gate, GoalGraphGate)
        assert gate.min_completion_pct == 0.5

    def test_unknown_type_raises(self):
        from horizonx.validators.registry import build_validator
        from horizonx.core.types import ValidatorConfig
        vc = ValidatorConfig(id="x", type="nonexistent_validator", config={})
        with pytest.raises(ValueError, match="unknown validator type"):
            build_validator(vc)
