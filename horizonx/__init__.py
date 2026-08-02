"""HorizonX — long-horizon agent execution harness.

See docs/LONG_HORIZON_AGENT.md for the full design.
"""

from horizonx.core.event_bus import Event, EventBus, InMemoryBus
from horizonx.core.runtime import Runtime
from horizonx.core.types import (
    AgentConfig,
    CumulativeMetrics,
    EnvironmentConfig,
    GoalNode,
    GoalStatus,
    HITLConfig,
    RepositoryConfig,
    ResourceLimits,
    Run,
    RunStatus,
    Session,
    SessionStatus,
    SpinDetectionConfig,
    Step,
    StepType,
    StrategyConfig,
    StrategyOutcome,
    SummarizerConfig,
    Task,
    ValidatorConfig,
)

__version__ = "0.1.0"

__all__ = [
    "Task",
    "Run",
    "Session",
    "Step",
    "StepType",
    "GoalNode",
    "GoalStatus",
    "RunStatus",
    "SessionStatus",
    "ResourceLimits",
    "CumulativeMetrics",
    "AgentConfig",
    "StrategyConfig",
    "StrategyOutcome",
    "EnvironmentConfig",
    "RepositoryConfig",
    "ValidatorConfig",
    "SummarizerConfig",
    "SpinDetectionConfig",
    "HITLConfig",
    "Runtime",
    "Event",
    "EventBus",
    "InMemoryBus",
]
