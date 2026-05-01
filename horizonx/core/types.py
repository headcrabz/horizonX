"""Core types for HorizonX.

These are the canonical data structures. Everything else derives from them.
See docs/LONG_HORIZON_AGENT.md §10 for the design rationale.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# ID types — string newtypes for clarity at call sites
# ---------------------------------------------------------------------------

def new_run_id() -> str:
    return f"run-{uuid4().hex[:12]}"


def new_session_id() -> str:
    return f"sess-{uuid4().hex[:12]}"


def new_step_id() -> str:
    return f"step-{uuid4().hex[:12]}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class HorizonClass(str, Enum):
    SHORT = "short"
    MEDIUM = "medium"
    LONG = "long"
    VERY_LONG = "very_long"
    CONTINUOUS = "continuous"


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED_HITL = "paused_hitl"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"
    TIMED_OUT = "timed_out"


class SessionStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    TIMEOUT = "timeout"
    OUT_OF_CONTEXT = "out_of_context"
    ERRORED = "errored"
    SPIN = "spin"


class GoalStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class StepType(str, Enum):
    THOUGHT = "thought"          # assistant text content / reasoning
    REASONING = "reasoning"      # explicit reasoning trace (Codex reasoning, Claude thinking)
    TOOL_CALL = "tool_call"      # tool invocation
    OBSERVATION = "observation"  # tool result
    FILE_CHANGE = "file_change"  # patch / diff event (Codex FileChange item)
    TODO_LIST = "todo_list"      # agent micro-plan snapshot (Codex TodoList; Claude TodoWrite)
    USAGE = "usage"              # token usage / cost report
    ERROR = "error"
    MILESTONE = "milestone"
    HITL_PAUSE = "hitl_pause"
    HITL_DECISION = "hitl_decision"
    SPIN = "spin"
    SESSION_ID = "session_id"    # captured agent session id for resume
    SYSTEM = "system"            # system / init event


class GateAction(str, Enum):
    CONTINUE = "continue"
    PAUSE_FOR_HITL = "pause_for_hitl"
    ABORT = "abort"
    RETRY_WITH_MOD = "retry_with_mod"


# ---------------------------------------------------------------------------
# Configuration sub-types
# ---------------------------------------------------------------------------


class ResourceLimits(BaseModel):
    """Hard caps. Run aborts when any is hit."""

    max_total_hours: float | None = 24.0
    max_total_tokens: int | None = 10_000_000
    max_total_usd: float | None = 100.0
    max_sessions: int | None = 200
    max_steps_per_session: int = 50
    max_minutes_per_session: float = 25.0
    max_tokens_per_session: int | None = None
    # Stall-timeout watchdog (0 = disabled)
    stall_soft_seconds: float = 120.0
    stall_hard_seconds: float = 300.0


class CumulativeMetrics(BaseModel):
    tokens_in: int = 0
    tokens_out: int = 0
    cache_creation_tokens: int = 0   # cache write
    cache_read_tokens: int = 0       # cache hit (cheap)
    usd: float = 0.0
    wall_seconds: float = 0.0
    sessions_count: int = 0
    steps_count: int = 0
    cache_hit_rate: float = 0.0      # derived


class AgentConfig(BaseModel):
    type: Literal["claude_code", "codex", "openhands", "custom", "mock"]
    model: str
    allowed_tools: list[str] | None = None
    thinking_budget: int | None = None
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    mcp_config_path: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class StrategyConfig(BaseModel):
    kind: Literal[
        "single",
        "sequential",
        "ralph",
        "tree",
        "monitor",
        "decomposition",
        "pair",
        "self_critique",
    ]
    config: dict[str, Any] = Field(default_factory=dict)


class EnvironmentConfig(BaseModel):
    type: Literal["podman", "docker", "local", "e2b"] = "local"
    image: str | None = None
    setup_commands: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    mounts: list[dict[str, str]] = Field(default_factory=list)


class ValidatorConfig(BaseModel):
    id: str
    type: str  # registered validator name
    runs: Literal[
        "after_every_session",
        "every_n_sessions",
        "before_destructive_action",
        "on_demand",
        "final",
    ] = "after_every_session"
    n: int | None = None  # for every_n_sessions
    weight: float = 1.0
    on_fail: Literal[
        "continue", "pause_for_hitl", "abort", "retry_with_modification"
    ] = "pause_for_hitl"
    config: dict[str, Any] = Field(default_factory=dict)


class SummarizerConfig(BaseModel):
    enabled: bool = True
    model: str = "claude-haiku-4-5"
    trigger_at_context_pct: float = 70.0
    max_tokens_per_summary: int = 2000


class SpinDetectionConfig(BaseModel):
    enabled: bool = True
    # ExactLoopLayer — hard abort; soft_exact_loop_threshold triggers a warning first
    exact_loop_threshold: int = 3
    soft_exact_loop_threshold: int = 2       # warn-and-inject before hard abort
    exact_loop_window: int = 20
    edit_revert_enabled: bool = True
    score_plateau_window: int = 3
    score_plateau_delta: float = 0.02
    semantic_layer_enabled: bool = True
    semantic_check_every_n_steps: int = 20
    semantic_model: str = "claude-haiku-4-5"
    # BucketedHashLayer — dual-threshold hash-bucket detector (tolerates minor variation)
    bucketed_hash_enabled: bool = True
    bucketed_hash_soft_threshold: int = 3
    bucketed_hash_hard_threshold: int = 5
    bucketed_hash_window: int = 30
    on_spin: Literal["terminate_and_retry", "terminate_and_hitl", "switch_strategy"] = (
        "terminate_and_hitl"
    )


class HITLConfig(BaseModel):
    enabled: bool = True
    triggers: list[str] = Field(
        default_factory=lambda: [
            "spin_detected",
            "validator_paused",
            "subgoal_max_attempts",
            "budget_threshold_75",
        ]
    )
    notification_type: Literal["slack", "email", "webhook", "console"] = "console"
    notification_target: str | None = None  # channel / email / url
    require_acknowledgement: bool = False


# ---------------------------------------------------------------------------
# Top-level types
# ---------------------------------------------------------------------------


class Task(BaseModel):
    """A user-facing description of work."""

    id: str
    name: str
    description: str = ""
    prompt: str
    horizon_class: HorizonClass = HorizonClass.LONG
    estimated_duration_hours: tuple[float, float] | None = None
    tags: list[str] = Field(default_factory=list)

    strategy: StrategyConfig
    agent: AgentConfig
    environment: EnvironmentConfig = Field(default_factory=EnvironmentConfig)
    milestone_validators: list[ValidatorConfig] = Field(default_factory=list)
    handoff_files: list[str] = Field(
        default_factory=lambda: [
            "progress.md",
            "goals.json",
            "decisions.jsonl",
            "failures.jsonl",
            "summary.md",
        ]
    )
    summarizer: SummarizerConfig = Field(default_factory=SummarizerConfig)
    spin_detection: SpinDetectionConfig = Field(default_factory=SpinDetectionConfig)
    hitl: HITLConfig = Field(default_factory=HITLConfig)
    resources: ResourceLimits = Field(default_factory=ResourceLimits)


class Run(BaseModel):
    id: str = Field(default_factory=new_run_id)
    parent_run_id: str | None = None  # for forks
    task: Task
    status: RunStatus = RunStatus.PENDING
    started_at: datetime = Field(default_factory=utcnow)
    completed_at: datetime | None = None
    workspace_path: Path
    current_session_id: str | None = None
    goal_graph_root: str = "g.root"
    cumulative: CumulativeMetrics = Field(default_factory=CumulativeMetrics)


class Session(BaseModel):
    id: str = Field(default_factory=new_session_id)
    run_id: str
    sequence_index: int
    target_goal_id: str | None = None
    status: SessionStatus = SessionStatus.RUNNING
    started_at: datetime = Field(default_factory=utcnow)
    completed_at: datetime | None = None
    steps_count: int = 0
    tokens_used: int = 0
    agent_session_id: str | None = None  # Claude Code / Codex session for resume
    handoff_summary_path: Path | None = None


class Step(BaseModel):
    id: str = Field(default_factory=new_step_id)
    session_id: str
    sequence: int
    type: StepType
    tool_name: str | None = None
    content: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=utcnow)
    duration_ms: int | None = None


class GoalNode(BaseModel):
    id: str
    parent_id: str | None = None
    name: str
    description: str
    verification_criteria: list[str] = Field(default_factory=list)
    status: GoalStatus = GoalStatus.PENDING
    children: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)  # goal IDs that must be DONE first
    attempts: int = 0
    max_attempts: int = 3
    progress_pct: float = 0.0   # 0-100, updated by agent via goals.json
    version: int = 0            # incremented on each status/notes mutation
    notes: str = ""
    last_updated_at: datetime = Field(default_factory=utcnow)
    last_updated_by_session: str | None = None

    @field_validator("id")
    @classmethod
    def _id_must_start_with_g(cls, v: str) -> str:
        if not v.startswith("g."):
            raise ValueError(f"goal id must start with 'g.': {v}")
        return v


# ---------------------------------------------------------------------------
# Validator output
# ---------------------------------------------------------------------------


class GateDecision(BaseModel):
    decision: GateAction
    reason: str
    score: float | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    suggested_modification: str | None = None
    validator_name: str
    duration_ms: int | None = None


class SessionRunResult(BaseModel):
    """Returned by an agent driver after a session completes."""

    agent_session_id: str | None = None
    status: SessionStatus
    error: str | None = None


class SpinReport(BaseModel):
    detected: bool
    layer: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    action: Literal[
        "none",
        "terminate_session_and_retry",
        "terminate_and_hitl",
        "switch_strategy",
        "warn_and_inject_diagnostic",
        "terminate_and_re_decompose",
    ] = "none"


class SessionSummary(BaseModel):
    session_id: str
    target_goal_id: str | None
    summary_md: str
    key_decisions: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    files_modified: list[str] = Field(default_factory=list)
    tests_status: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 0.5


class HITLDecision(BaseModel):
    action: Literal["approve", "modify", "abort", "re_decompose"]
    instruction: str = ""
    operator: str | None = None
    decided_at: datetime = Field(default_factory=utcnow)
