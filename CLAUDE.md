# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install for development
pip install -e ".[dev]"

# Install with optional extras
pip install -e ".[dev,dashboard,slack]"

# Run all tests
pytest

# Run a single test file
pytest tests/test_runtime_integration.py

# Run a single test by name
pytest tests/test_runtime_integration.py::test_name -v

# Lint
ruff check horizonx/ tests/

# Type check
mypy horizonx/

# Run the CLI
horizonx run examples/self_critique/task.yaml
horizonx run task.yaml --resume <run-id>
horizonx list
horizonx show <run-id>
horizonx watch <run-id>
horizonx export <run-id> --format json
horizonx serve  # requires horizonx[dashboard]

# Override DB path (default: horizonx.db)
horizonx --db /path/to/custom.db run task.yaml
HORIZONX_DB=/path/to/db horizonx list
```

## Architecture

HorizonX is a **long-horizon agent execution harness** — not an agent itself. It wraps external agent CLIs (Claude Code, Codex, OpenHands) with durability, observability, and failure recovery infrastructure.

### Data flow

```
Task (YAML / Python) → Runtime.run()
  → Strategy.execute()   (decides session loop shape)
    → Agent.run_session()  (spawns subprocess, yields Steps)
      → TrajectoryRecorder  (persists Steps to DB + JSONL)
    → Runtime.run_validators()  (gates: continue / pause / abort)
    → Runtime.check_spin()      (6-layer loop detector)
    → Runtime.summarize()       (compress context at 70%)
  → EventBus  (SSE/WebSocket to dashboard / CLI watch)
  → SqliteStore  (all state persisted — crash-safe)
```

### Key types (`horizonx/core/types.py`)

All canonical data structures are Pydantic v2 models here. The most important:

- **`Task`** — user-facing spec: `id`, `prompt`, `strategy`, `agent`, `milestone_validators`, `handoff_files`, `spin_detection`, `hitl`, `resources`. Loaded from YAML via `Task.model_validate(yaml_dict)`.
- **`Run`** — live execution state: `id`, `status`, `workspace_path`, `cumulative` metrics, `current_session_id`.
- **`Session`** — one bounded agent invocation within a run. Each session has a `target_goal_id` and records `steps_count`, `tokens_used`, `agent_session_id` (for resume).
- **`Step`** — atomic trajectory event: `type` (StepType enum), `tool_name`, `content`.
- **`GoalNode`** — node in the goal DAG. IDs must start with `g.`. Status is monotonic: PENDING → IN_PROGRESS → DONE/FAILED/SKIPPED.
- **`GateDecision`** — validator output: a `GateAction` (CONTINUE / PAUSE_FOR_HITL / ABORT / RETRY_WITH_MOD) plus `reason` and optional `score`.

### Extension points

**Strategies** (`horizonx/strategies/`): Implement `Strategy` protocol — `async def execute(run, rt) -> AsyncIterator[Event]`. Registered via `pyproject.toml` entry points (`horizonx.strategies`). Available: `single`, `sequential`, `ralph`, `tree`, `monitor`, `decomposition`, `pair`, `self_critique`.

**Agent drivers** (`horizonx/agents/`): Implement `BaseAgent` protocol — `async def run_session(prompt, workspace, *, resume_session_id, on_step, cancel_token) -> SessionRunResult`. The `on_step` callback is how steps reach the recorder. Registered via entry points (`horizonx.agents`). Available: `claude_code`, `codex`, `openhands`, `custom`, `mock` (for tests).

**Validators** (`horizonx/validators/`): Implement `BaseValidator` protocol — `async def validate(run, session, workspace) -> GateDecision`. Must return a `GateAction` decision (not a score). Registered via entry points (`horizonx.validators`). Available: `test_suite`, `shell`, `llm_judge`, `metric`, `goal_graph`.

### Goal graph (`horizonx/core/goal_graph.py`)

The `GoalGraph` is the single source of truth for task progress. It persists as `goals.json` in the run's workspace and is loaded at the start of every session. The agent can only **propose** goals as done; the runtime **accepts** only after validators pass. All IDs must start with `g.` — enforced by Pydantic validator.

### Storage (`horizonx/storage/sqlite.py`)

`SqliteStore` uses synchronous `sqlite3` (not async despite `aiosqlite` being listed as a dependency — the async migration is planned). Tables: `runs`, `sessions`, `steps`, `goals`, `validations`, `hitl_events`, `spin_reports`. The `task_snapshot` column stores the full `Task` JSON so runs are self-contained.

### Workspace layout

Each run gets its own directory under `horizonx-workspaces/<task-id>-<8-char-id>/`. Handoff files written there (`progress.md`, `goals.json`, `decisions.jsonl`, `failures.jsonl`, `summary.md`) are injected into the agent prompt at each session start.

### Dashboard (`horizonx/dashboard/`)

Optional FastAPI app (requires `horizonx[dashboard]`). Routes split by concern: `routes_runs.py`, `routes_events.py` (SSE stream), `routes_hitl.py`, `routes_launch.py`. Static frontend served from `dashboard/static/`.

### Testing

Tests use `pytest-asyncio` with `asyncio_mode = "auto"` — no `@pytest.mark.asyncio` needed. The `mock` agent type (`horizonx/agents/mock.py`) is used for unit tests. Core fixtures in `tests/conftest.py`: `rt` (Runtime with temp DB), `store` (SqliteStore), `mock_task` (minimal Task). The `ANTHROPIC_API_KEY` is not required to run tests.
