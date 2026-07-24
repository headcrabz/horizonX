# HorizonX — Task Tracker

> Single source of truth for all build tasks. Every task has a definition of done and explicit validation steps.
> Update **Status** as work progresses. Do not mark Done until all validation steps pass.

---

## Progress Summary

| Phase | Tasks | Not Started | In Progress | Review | Done |
|---|---|---|---|---|---|
| Phase 0 — Bug Fixes | 3 | 0 | 0 | 0 | 3 |
| Phase 1 — Production Reliability | 4 | 0 | 0 | 0 | 4 |
| Phase 2 — Governance + Ecosystem | 6 | 2 | 0 | 0 | 4 |
| Phase 3 — Memory + Multi-Agent | 11 | 6 | 0 | 0 | 5 |
| **Total** | **24** | **8** | **0** | **0** | **16** |

---

## Status Legend

| Status | Meaning |
|---|---|
| `Not Started` | Work not begun |
| `In Progress` | Actively being built |
| `Review` | Built; awaiting validation steps |
| `Done` | All validation steps confirmed |
| `Blocked` | Waiting on a dependency |

---

## How to Use This File

1. Pick the highest-priority `Not Started` task whose dependencies are `Done`.
2. Set status to `In Progress`.
3. Follow the **Files to modify / create** section precisely.
4. Run every step in **Validation** before marking `Review`.
5. After another pair of eyes confirms the validation steps pass, mark `Done`.
6. Update the Progress Summary table.

---

---

# Phase 0 — Bug Fixes

*These are correctness defects in existing code. Ship before any new features.*

---

### HX-01 · Wire ResourceGovernor: charge() to token events + HITL at 75%

**Priority:** P0 | **Status:** Done | **Effort:** 2 days
**Gap Ref:** GAP-01 | **Depends on:** none
**Reviewer note:** Root cause was deeper than planned — `charge()` was never called in production. Two bugs fixed, not one.

**What to build:**
`ResourceGovernor._check_thresholds` publishes a `budget.threshold` event at 75% but never calls `runtime.request_hitl`. The run hard-crashes at 100% instead of pausing for human review. Fix the wire-up.

**Files to modify:**
- `horizonx/core/governor.py` — add `hitl_callback: Callable | None` parameter to `__init__`; in `_check_thresholds`, when utilisation ≥ 0.75 and `HITLConfig.triggers` contains `"budget_threshold_75"`, call `await self.hitl_callback(run, "budget_threshold_75", {...})` before publishing the event
- `horizonx/core/runtime.py` — pass `hitl_callback=self.request_hitl` when constructing `ResourceGovernor` inside `_governor()`
- `horizonx/core/types.py` — confirm `HITLConfig.triggers` field already includes `"budget_threshold_75"` (it does at line 220; no change needed)

**Definition of done:**
- [ ] A run with `max_total_usd: 1.0` and `hitl.triggers: [budget_threshold_75]` pauses at `$0.75` with `run.status == "PAUSED_HITL"`
- [ ] The `hitl_events` table records the event with `trigger = "budget_threshold_75"`
- [ ] Run resumes correctly after HITL decision `approve`
- [ ] Run still hard-aborts at 100% (existing `BudgetExceeded` path unchanged)
- [ ] Existing `test_governor_*` tests still pass

**Validation steps:**
```bash
pytest tests/ -k "governor" -v
pytest tests/ -k "hitl" -v
# Manual: run a mock task with max_total_usd=0.01, confirm it pauses before crashing
horizonx run examples/demo_word_counter/task.yaml  # should pause at 75% if budget set low
```

---

### HX-02 · Fix validator registry to use entry-points

**Priority:** P0 | **Status:** Done | **Effort:** 0.5 days
**Gap Ref:** GAP-02 | **Depends on:** none

**What to build:**
`validators/registry.py` uses a hardcoded `if/elif` chain. Third-party validators installed via `pip install` are silently ignored. Replace with `importlib.metadata.entry_points` lookup, keeping built-ins as a fast path.

**Files to modify:**
- `horizonx/validators/registry.py` — rewrite `build_validator()`:
  1. Define `_BUILTIN_MAP: dict[str, str]` mapping type names to import paths (e.g. `"shell": "horizonx.validators.shell:ShellGate"`)
  2. Try the builtin map first (fast path, no entry-point overhead)
  3. Fall back to `importlib.metadata.entry_points(group="horizonx.validators")` — find by `.name == vc.type`, call `.load()`, instantiate with `(cfg, store=store)`
  4. Raise `ValueError` only if neither path resolves

**Files to confirm (no change needed):**
- `pyproject.toml` — `[project.entry-points."horizonx.validators"]` already declares all 6 built-ins; verify names match the builtin map keys

**Definition of done:**
- [ ] A dummy third-party validator package (a single `.py` file with correct entry-point metadata) is loadable via `build_validator(ValidatorConfig(type="dummy_validator", ...))`
- [ ] All 6 existing built-in validator types still resolve correctly
- [ ] `pytest tests/ -k validator` passes
- [ ] `build_validator(ValidatorConfig(type="unknown_xyz"))` still raises `ValueError`

**Validation steps:**
```bash
# Create a minimal test package inline:
# horizonx/validators/dummy.py — a valid BaseValidator subclass
# Add entry-point to pyproject.toml under [project.entry-points."horizonx.validators"]
# dummy_validator = "horizonx.validators.dummy:DummyGate"
pip install -e .
pytest tests/test_validators.py -v
python -c "
from horizonx.validators.registry import build_validator
from horizonx.core.types import ValidatorConfig
v = build_validator(ValidatorConfig(type='dummy_validator', id='t', runs='on_demand', on_fail='abort', config={}))
print('OK:', v)
"
```

---

### HX-03 · Fix SQLite store sync-blocking in async methods

**Priority:** P0 | **Status:** Done | **Effort:** 2 days
**Gap Ref:** GAP-05 | **Depends on:** none

**What to build:**
`SqliteStore` methods are `async def` but call `sqlite3.connect()` synchronously, blocking the asyncio event loop. Under `tree` strategy with parallel branches, this serializes all DB writes. Wrap every DB call in a dedicated `ThreadPoolExecutor`.

**Files to modify:**
- `horizonx/storage/sqlite.py`:
  1. Add `self._executor = ThreadPoolExecutor(max_workers=1)` in `__init__` (single thread to avoid SQLite concurrent-write issues)
  2. Extract all `with sqlite3.connect(...) as conn: ...` blocks into private sync methods (`_sync_save_run`, `_sync_save_step`, etc.)
  3. In each `async def save_*` / `async def list_*` method, replace the direct sqlite3 call with: `await asyncio.get_event_loop().run_in_executor(self._executor, self._sync_save_run, run)`
  4. Add `async def close(self)` that calls `self._executor.shutdown(wait=True)` — called by `Runtime.__aexit__`

**Definition of done:**
- [ ] `tree` strategy with `width: 4` runs with all 4 branches writing steps concurrently — no `asyncio` blocking warnings in `asyncio.debug` mode
- [ ] All existing storage tests pass: `pytest tests/ -k "store or sqlite" -v`
- [ ] `SqliteStore` can handle 10 concurrent `asyncio.gather` calls to `save_step` without deadlock
- [ ] `close()` is called at run teardown (checked via `Runtime` lifecycle)

**Validation steps:**
```bash
# Enable asyncio debug to catch blocking calls
PYTHONASYNCIODEBUG=1 pytest tests/ -k "store" -v
# Run tree strategy test
pytest tests/ -k "tree" -v
# Concurrency stress test (write one yourself if missing):
python -c "
import asyncio
from horizonx.storage.sqlite import SqliteStore
async def main():
    store = SqliteStore(':memory:')
    # 10 concurrent writes
    await asyncio.gather(*[store.save_step(...) for _ in range(10)])
    await store.close()
asyncio.run(main())
"
```

---

---

# Phase 1 — Production Reliability

*Makes HorizonX viable for real unattended team use.*

---

### HX-04 · Real Slack HITL + webhook retry + timeout escalation

**Priority:** P1 | **Status:** Done | **Effort:** 3 days
**Gap Ref:** GAP-03 | **Depends on:** HX-01

**What to build:**
`hitl/gate.py` Slack integration is a `sys.stderr.write` stub. Webhook has `timeout=5s` with `except Exception: pass`. File polling never times out. Replace with real Slack Block Kit notifications, exponential retry on webhooks, and timeout-based escalation.

**Files to modify:**
- `horizonx/hitl/gate.py`:
  - `_notify_slack`: implement using `slack_sdk.web.async_client.AsyncWebClient`. Post a Block Kit message: run ID, reason, truncated context, dashboard link (if configured), approve/modify/abort buttons as Block Kit actions. Requires `HORIZONX_SLACK_TOKEN` env var.
  - `_notify_webhook`: replace `timeout=5s` with 3 attempts at 5/15/30s backoff using `asyncio.sleep`. Log each failure. Raise after 3rd failure.
  - `await_decision` polling loop: add `cfg.timeout_minutes` check — if elapsed > timeout and `cfg.escalation_action` is set, either auto-resolve or escalate to secondary channel. Add configurable `max_wait_minutes` (default: None = wait forever).
- `horizonx/dashboard/routes_hitl.py`:
  - `POST /api/runs/{run_id}/hitl` currently writes `.hitl_decision.json` — also publish an `SSE` event via the bus so the polling coroutine wakes immediately instead of waiting 2s.
- `horizonx/core/types.py`:
  - Add `HITLConfig` fields: `timeout_minutes: int | None = None`, `escalation_channel: str | None = None`, `escalation_action: Literal["approve", "abort"] | None = None`

**Files to create:**
- `horizonx/hitl/slack.py` — extracted Slack client wrapper with Block Kit card builder. Keeps `gate.py` clean.

**Definition of done:**
- [ ] With a valid `HORIZONX_SLACK_TOKEN`, a test run posting HITL sends a real Slack message to the configured channel with a formatted Block Kit card
- [ ] Webhook with a failing URL retries 3 times (logged) and then raises, not silently fails
- [ ] A run with `timeout_minutes: 1` and `escalation_action: approve` auto-approves after 60 seconds if no operator response
- [ ] Dashboard HITL resolution wakes the waiting coroutine within 1 second (not after 2s poll)
- [ ] `pytest tests/ -k "hitl" -v` passes (add mock Slack client in tests)

**Validation steps:**
```bash
pytest tests/test_hitl.py -v
# Integration test with real Slack (requires token in env):
HORIZONX_SLACK_TOKEN=xoxb-... pytest tests/test_hitl_slack.py -v -m integration
# Timeout escalation test:
python -c "
import asyncio, os
os.environ['HORIZONX_HITL_TIMEOUT_TEST'] = '1'
# Run a task that hits HITL, confirm it auto-approves after 1 minute
"
```

---

### HX-05 · Cross-session spin detection via persistent state

**Priority:** P1 | **Status:** Done | **Effort:** 2 days
**Gap Ref:** GAP-04 | **Depends on:** HX-03

**What to build:**
`SpinDetector` layers only query `store.recent_steps(session.id, window)` — current session only. A run where each session makes one superficially plausible change but no goal transitions occur over N sessions won't be caught. Add a `CrossSessionSpinLayer` that queries the `goals` and `validations` tables across sessions.

**Files to modify:**
- `horizonx/core/spin_detector.py`:
  - Add `CrossSessionSpinLayer` class at the end. Logic: query the last `window` sessions for this run (default 8). If: (a) no `GoalNode` status has changed from `PENDING` to `DONE` in those sessions, AND (b) all validator scores where non-null have variance < `plateau_variance` (default 0.02) — fire `SpinReport(detected=True, layer="cross_session", ...)`.
  - Add it as the 7th layer in `SpinDetector.layers` (after `SemanticProgressLayer`, most expensive → last).
- `horizonx/core/runtime.py`:
  - `check_spin` currently instantiates `SpinDetector` fresh each call — keep that for in-session layers. For `CrossSessionSpinLayer`, pass `run_id` not just `session_id`, so the layer can query across sessions.
- `horizonx/storage/sqlite.py`:
  - Add `async def goal_status_history(run_id: str, last_n_sessions: int) -> list[dict]` — returns goal status at end of each of the last N sessions (join `sessions` + `goals` on `last_updated_by_session`).
  - Add `async def recent_validator_scores_cross_session(run_id: str, last_n_sessions: int) -> list[float]` — aggregate scores across sessions.

**Definition of done:**
- [ ] A mock run with 10 sessions where every session exits with `status=COMPLETED` but no goal transitions to `DONE` triggers `CrossSessionSpinLayer` after `window=8` sessions
- [ ] A healthy run with goals completing normally does NOT trigger the layer
- [ ] `pytest tests/ -k "spin" -v` passes including new cross-session tests
- [ ] `spin_reports` table records the cross-session fire with `layer="cross_session"`

**Validation steps:**
```bash
pytest tests/test_spin_detector.py -v
# Add a test: mock_task with 10 sessions, all COMPLETED, no goals marked DONE
# Assert SpinReport.layer == "cross_session" fires on session 9
```

---

### HX-06 · FTS5 relevance-based context injection in SessionManager

**Priority:** P1 | **Status:** Done | **Effort:** 3 days
**Gap Ref:** GAP-06 | **Depends on:** HX-03

**What to build:**
`SessionManager.compose_prompt` injects the last-20 `decisions.jsonl` entries (hardcoded recency). Replace with FTS5 full-text search over all decisions in the run, ranked by relevance to the current goal, capped at 4000 tokens.

**Files to create:**
- `horizonx/core/knowledge.py` — `RunKnowledgeStore` class:
  - `__init__(workspace: Path, run_id: str)` — opens/creates `knowledge.db` (SQLite FTS5) in workspace
  - Schema: `CREATE VIRTUAL TABLE decisions_fts USING fts5(content, goal_id, ts, tokenize="unicode61")`
  - `async def index_decisions(jsonl_path: Path)` — reads `decisions.jsonl` line by line, inserts into FTS5. Called after each session end.
  - `async def search(query: str, limit: int = 15) -> list[str]` — `SELECT content FROM decisions_fts WHERE decisions_fts MATCH ? ORDER BY rank LIMIT ?`. Falls back to recency tail if FTS returns 0 results.
  - `async def recent(n: int = 5) -> list[str]` — last N decisions regardless of relevance (recency anchor).

**Files to modify:**
- `horizonx/core/session_manager.py`:
  - Replace `self._tail_jsonl("decisions.jsonl", 20)` with: `await RunKnowledgeStore(self.workspace, self.run.id).search(query=f"{target_goal.name} {target_goal.description}", limit=15)` + `recent(5)` as a recency anchor.
  - Add `_count_tokens(text: str) -> int` helper (chars/4 estimate). Cap injected content at 4000 tokens total; if over, drop oldest FTS hits first.
  - Import `RunKnowledgeStore` from `horizonx.core.knowledge`.
- `horizonx/strategies/sequential.py`:
  - After `rt.end_session(session, ...)`, call `await knowledge_store.index_decisions(workspace / "decisions.jsonl")` to keep the FTS index current.

**Definition of done:**
- [ ] Session prompt for goal `g.auth.jwt` retrieves decisions mentioning "JWT", "token", "auth" — not the 20 most recent unrelated decisions
- [ ] `knowledge.db` is created in the workspace on first session
- [ ] Falls back to recency tail gracefully if `knowledge.db` doesn't exist yet (first session)
- [ ] Token cap at 4000 is enforced — tested with a workspace that has 500 decisions
- [ ] `pytest tests/ -k "session_manager or knowledge" -v` passes

**Validation steps:**
```bash
pytest tests/test_session_manager.py -v
# Inject 200 dummy decisions, 5 matching a specific goal keyword
# Assert compose_prompt returns those 5 + last-5 recency anchor
# Assert total injected decisions tokens < 4000
```

---

### HX-07 · Usage policy enforcer — workspace budgets + cost velocity

**Priority:** P1 | **Status:** Done | **Effort:** 2 days
**Gap Ref:** GAP-08 | **Depends on:** HX-01, HX-03

**What to build:**
`ResourceGovernor` only tracks per-run limits. Add cross-run workspace-level daily budget tracking and cost velocity detection (tokens/minute rising exponentially = runaway signal).

**Files to create:**
- `horizonx/core/usage.py` — `UsageStore` class:
  - Schema (appended to existing SQLite DB): `CREATE TABLE workspace_usage (workspace_id TEXT, date TEXT, run_id TEXT, tokens_in INT, tokens_out INT, usd REAL, recorded_at TIMESTAMP)` + index on `(workspace_id, date)`.
  - `async def record(workspace_id, run_id, tokens_in, tokens_out, usd)` — INSERT row.
  - `async def daily_total(workspace_id, date) -> dict` — SUM across all runs for that workspace on that date.
  - `async def check_daily_budget(workspace_id, daily_budget_usd) -> bool` — returns `True` if today's total ≥ budget.
- `horizonx/core/velocity.py` — `CostVelocityMonitor`:
  - Maintains a sliding window of the last 5 `charge()` calls with timestamps.
  - `record(usd: float, tokens: int)` — append with `time.monotonic()`.
  - `is_runaway(threshold_usd_per_min: float = 1.0) -> bool` — compute slope of usd/time over window; return `True` if slope doubled twice in succession.

**Files to modify:**
- `horizonx/core/types.py`:
  - Add `WorkspaceConfig` model: `workspace_id: str`, `daily_budget_usd: float | None = None`, `max_concurrent_runs: int = 5`.
  - Add `workspace: WorkspaceConfig | None = None` field to `Task`.
- `horizonx/core/governor.py`:
  - Accept `usage_store: UsageStore | None` and `velocity_monitor: CostVelocityMonitor | None` in `__init__`.
  - In `charge()`: also call `usage_store.record(...)` if set; call `velocity_monitor.record(...)` if set.
  - In `_check_thresholds()`: check `velocity_monitor.is_runaway()` and if true, publish `budget.velocity_alert` event and trigger HITL (same callback as HX-01).
  - Check `usage_store.check_daily_budget(workspace_id, daily_budget_usd)` — if exceeded, raise `BudgetExceeded("daily workspace budget exceeded")`.
- `horizonx/core/runtime.py`:
  - When `task.workspace` is set, construct `UsageStore` and `CostVelocityMonitor` and pass to `ResourceGovernor`.

**Definition of done:**
- [ ] Two runs in the same workspace totalling > `daily_budget_usd` — the second run raises `BudgetExceeded` before it can proceed past the governor
- [ ] A run whose `usd/minute` doubles twice in 5 charge() calls triggers a HITL pause with reason `"velocity_alert"`
- [ ] `workspace_usage` table is populated after each run
- [ ] `pytest tests/ -k "usage or velocity" -v` passes

**Validation steps:**
```bash
pytest tests/test_usage.py -v
pytest tests/test_velocity.py -v
# Integration: create two mock runs in same workspace, second should hit daily budget
```

---

---

# Phase 2 — Governance + Open Source Ecosystem

*Makes HorizonX appropriate for teams and publishable as open source.*

---

### HX-08 · PolicyEngine — T1 Python callable policies

**Priority:** P1 | **Status:** Done | **Effort:** 5 days
**Gap Ref:** GAP-07 | **Depends on:** HX-01, HX-02, HX-03

**What to build:**
A `PolicyEngine` that evaluates registered policies at three phases: task intake, session start, and step emit. Each policy is a Python callable returning `PolicyDecision(action, reason)`. Five built-in policies included.

**Files to create:**
- `horizonx/governance/__init__.py`
- `horizonx/governance/engine.py` — `PolicyEngine`:
  - `phases: list[PolicyPhase]` = `["task_intake", "session_start", "step_emit"]`
  - `actions: list[PolicyAction]` = `["allow", "warn", "block", "hitl"]`
  - `register(policy: BasePolicy, phase: PolicyPhase)` — add to internal registry
  - `async def evaluate(phase, context: PolicyContext) -> PolicyDecision` — run all registered policies for phase; aggregate (strictest action wins); `"block"` and `"hitl"` are fail-closed
  - `PolicyContext` dataclass: `run, session, step, task, workspace_config`
  - Load policies from `horizonx.policies` entry-point group (same pattern as validators)
- `horizonx/governance/base.py` — `BasePolicy` protocol:
  ```python
  class BasePolicy(Protocol):
      name: str
      phase: PolicyPhase
      async def evaluate(self, ctx: PolicyContext) -> PolicyDecision: ...
  ```
- `horizonx/governance/policies/branch_guard.py` — blocks sessions targeting git branches matching a deny-list pattern (`protected_branches: list[str]`)
- `horizonx/governance/policies/file_delete_guard.py` — routes `step_emit` phase to HITL if `step.tool_name in ("Bash",)` and `step.content` matches `rm -rf` / `git clean` / `truncate` patterns
- `horizonx/governance/policies/agent_allowlist.py` — blocks task intake if `task.agent.type not in allowed_agents` for a given `task.tag`
- `horizonx/governance/policies/environment_tag.py` — blocks task intake if `workspace.environment_tag == "production"` and task not approved
- `horizonx/governance/policies/cost_velocity.py` — delegates to `CostVelocityMonitor` from HX-07; triggers HITL if runaway

**Files to modify:**
- `horizonx/core/runtime.py`:
  - Add `self.policy_engine = PolicyEngine()` in `__init__`
  - Call `await self.policy_engine.evaluate("task_intake", ctx)` at start of `run()`
  - Call `await self.policy_engine.evaluate("session_start", ctx)` in `start_session()`
  - In `TrajectoryRecorder.on_step` callback, call `await self.policy_engine.evaluate("step_emit", ctx)` — non-blocking warn, blocking for block/hitl actions
- `pyproject.toml`:
  - Add `[project.entry-points."horizonx.policies"]` section with 5 built-in policies

**Definition of done:**
- [ ] A task with `agent.type: codex` blocked by `AgentAllowlistPolicy(allowed=["claude_code"])` raises `PolicyViolation` at task intake
- [ ] A step with `rm -rf /workspace` triggers `FileDeleteGuardPolicy` → HITL
- [ ] A third-party policy installed via pip is loaded by `PolicyEngine` via entry-points
- [ ] `warn` action logs without blocking execution
- [ ] `pytest tests/test_governance.py -v` passes

**Validation steps:**
```bash
pytest tests/test_governance.py -v
pytest tests/ -k "policy" -v
# Verify all 5 built-in policies evaluate correctly with mock contexts
```

---

### HX-09 · Z3 SMT constraint policies (optional T2 tier)

**Priority:** P2 | **Status:** Done | **Effort:** 3 days
**Gap Ref:** GAP-07 extension | **Depends on:** HX-08

**What to build:**
Optional `horizonx[z3]` extra that adds a `Z3ConstraintPolicy` type. Business rules expressible as SMT constraints (formally verifiable, zero API cost, audit-friendly). Policies declared in YAML as Z3 logical expressions.

**Files to create:**
- `horizonx/governance/policies/z3_constraint.py`:
  - `Z3ConstraintPolicy(BasePolicy)`:
    - `formula: str` — Z3 Python expression as a string, evaluated in a sandboxed `exec` context with Z3 `Bool`, `String`, `And`, `Or`, `Not`, `Implies` in scope
    - `context_bindings: dict[str, str]` — maps Z3 variable names to `PolicyContext` fields (e.g. `{"tag": "ctx.task.tags[0]", "agent": "ctx.task.agent.type"}`)
    - `evaluate(ctx)`: bind variables, run `Solver().check()`, return `allow` if `sat`, `block` if `unsat`
    - Sandbox: `exec` with a restricted globals dict containing only Z3 symbols — no `__import__`, no builtins
  - Include a Z3 availability guard: `try: import z3 except ImportError: raise ImportError("install horizonx[z3]")`

**Files to modify:**
- `pyproject.toml`:
  - Add `z3 = ["z3-solver>=4.13"]` to `[project.optional-dependencies]`
  - Add `z3_constraint = "horizonx.governance.policies.z3_constraint:Z3ConstraintPolicy"` to `horizonx.policies` entry-points (only active when z3 installed)
- `horizonx/core/types.py`:
  - `ValidatorConfig` and `PolicyConfig` should support `type: "z3_constraint"` without code change (entry-points handles it)

**Example YAML usage (to be documented):**
```yaml
policies:
  - type: z3_constraint
    phase: task_intake
    on_fail: block
    formula: |
      And(
        Or(tag == "safe", tag == "reviewed"),
        agent != "codex",
        env != "production"
      )
    context_bindings:
      tag: "ctx.task.tags[0] if ctx.task.tags else ''"
      agent: "ctx.task.agent.type"
      env: "ctx.run.workspace_config.environment_tag or ''"
```

**Definition of done:**
- [ ] A policy with an `unsat` formula blocks task intake with `reason` containing the formula
- [ ] A policy with a `sat` formula allows through
- [ ] Invalid formula syntax raises `PolicyConfigError` at policy load time, not at evaluation time
- [ ] `exec` sandbox cannot call `__import__` or access filesystem (test with an adversarial formula)
- [ ] `pip install horizonx[z3]` installs `z3-solver` correctly
- [ ] `pytest tests/test_z3_policy.py -v` passes (mocked when `z3` not installed; real when installed)

**Validation steps:**
```bash
pip install -e ".[z3]"
pytest tests/test_z3_policy.py -v
# Adversarial sandbox test:
python -c "
from horizonx.governance.policies.z3_constraint import Z3ConstraintPolicy
p = Z3ConstraintPolicy(formula='__import__(\"os\").system(\"id\")', context_bindings={})
# Should raise PolicyConfigError or evaluate safely without executing the import
"
```

---

### HX-10 · Implement re_decompose HITL action

**Priority:** P2 | **Status:** Done | **Effort:** 2 days
**Gap Ref:** GAP-09 | **Depends on:** HX-01
**Reviewer corrections:** (1) Dependency on HX-04 removed — re_decompose works with console HITL, Slack is convenience not requirement. (2) `rt.llm_client` does not exist on Runtime — use `call_llm_json` from `horizonx/core/llm_client.py` directly, not via runtime. (3) Current code appends a note to goal.notes and continues — it does NOT "fall through to approve" as the original plan claimed.

**What to build:**
`sequential.py:211` has `# re_decompose: not yet implemented`. When an operator selects re_decompose at HITL and provides an instruction, call `LLMClient` to restructure the pending/failed goals in `goals.json` according to the instruction. Preserve `DONE` goals.

**Files to modify:**
- `horizonx/strategies/sequential.py`:
  - In the HITL decision handler, add `elif decision.action == "re_decompose": await self._re_decompose(run, rt, decision.instruction)`
  - Implement `_re_decompose(run, rt, instruction: str)`:
    1. Load current `goals.json` via `GoalGraph.load(workspace / "goals.json")`
    2. Extract `PENDING` and `IN_PROGRESS` nodes (leave `DONE` nodes untouched)
    3. Build LLM prompt: system = "You are a task decomposition expert. Given a goal graph and operator feedback, restructure ONLY the pending/in_progress goals. Return a valid `goals.json` JSON object. Preserve all DONE goals exactly as given."
    4. Call `rt.llm_client.complete(prompt)` (Haiku model, cheap)
    5. Parse JSON response, validate with `GoalGraph.validate()`
    6. Write back to `goals.json` and update `goals` table via `store.sync_goals(run.id, new_graph)`
    7. Emit `Event(type="goals.re_decomposed", run_id=run.id, payload={"instruction": instruction})`
- `horizonx/core/runtime.py`:
  - Confirm `llm_client` is accessible from strategies via `rt.llm_client` (it exists as `self.llm` — expose as property)

**Definition of done:**
- [ ] After re_decompose with instruction "split g.auth into 3 smaller goals", the workspace `goals.json` contains 3 new pending goals under `g.auth`
- [ ] `DONE` goals are unchanged after re_decompose
- [ ] Invalid LLM JSON response triggers a retry (up to 2 attempts) then falls through to `approve`
- [ ] The `goals` SQLite table reflects the new graph
- [ ] `pytest tests/ -k "re_decompose" -v` passes

**Validation steps:**
```bash
pytest tests/test_sequential.py -k "re_decompose" -v
# Mock LLMClient to return a valid restructured goals.json
# Assert: new goals exist, DONE goals preserved, goals table updated
```

---

### HX-11 · Durable dashboard launch — survive process restart

**Priority:** P2 | **Status:** Done | **Effort:** 2 days
**Gap Ref:** GAP-11 | **Depends on:** HX-03

**What to build:**
`routes_launch.py` fires `asyncio.create_task(runtime.run(...))` — a run launched via the dashboard is lost on process restart. Add a `pending_runs` table and startup recovery.

**Files to create:**
- `horizonx/dashboard/recovery.py` — `RunRecovery`:
  - `async def save_pending(run_id, task_json)` — INSERT into `pending_runs` table
  - `async def mark_started(run_id)` — UPDATE status = "started"
  - `async def mark_done(run_id)` — DELETE row (run completed or failed)
  - `async def recover_pending(runtime)` — query `pending_runs WHERE status = "pending"`, re-launch each via `asyncio.create_task(runtime.run(Task.model_validate_json(task_json), resume_from=run_id))`

**Files to modify:**
- `horizonx/storage/sqlite.py`:
  - Add `pending_runs` table to schema: `(run_id TEXT PK, task_json TEXT, status TEXT, created_at TIMESTAMP)`
  - Add `save_pending_run`, `mark_pending_run_started`, `delete_pending_run` methods
- `horizonx/dashboard/routes_launch.py`:
  - Before `asyncio.create_task(...)`, call `await store.save_pending_run(run.id, task.model_dump_json())`
  - Wrap the task coroutine to call `store.delete_pending_run(run.id)` on completion/failure
- `horizonx/dashboard/app.py`:
  - In `create_app()` startup, call `await RunRecovery(store).recover_pending(runtime)`

**Definition of done:**
- [ ] Launch a run via dashboard, kill the process, restart — run resumes from the last session boundary
- [ ] Completed runs are removed from `pending_runs` table
- [ ] `pending_runs` table is queried on every startup (not just first)
- [ ] `pytest tests/test_dashboard_routes.py -v` passes (existing test file already in repo)

**Validation steps:**
```bash
pytest tests/test_dashboard_routes.py -v
# Manual: start horizonx serve, POST /api/runs, kill server, restart, check run resumes
```

---

### HX-12 · Fix agent dispatch to use entry-points + add SDK driver

**Priority:** P2 | **Status:** Done | **Effort:** 2 days
**Gap Ref:** GAP-12 | **Depends on:** HX-02
**Reviewer correction:** `horizonx.agents` entry-point group already existed in pyproject.toml. Two real gaps: (1) `_build_agent` in strategies used if/elif ignoring entry-points; (2) `AgentConfig.type` Literal blocked third-party types at Pydantic level. Both partially fixed in HX-01 work (sequential.py + types.py). Remaining: apply entry-point dispatch to all other strategies (pair, tree, self_critique, decomposition, monitor) + add SDK driver + template.

**What to build:**
Agent drivers are not loadable via `pip install` (no `horizonx.agents` entry-point group). Add entry-point registration, update runtime loading, and add a `sdk` driver for Python-callable agents (no subprocess shim required).

**Files to create:**
- `horizonx/agents/sdk.py` — `SDKAgent(BaseAgent)`:
  - `__init__(callable: Callable[[str, Path], AsyncIterator[Step]])` — accepts any async generator
  - `run_session(prompt, workspace, ...)` — calls `callable(prompt, workspace.path)`, iterates steps, calls `on_step(step)`, returns `SessionRunResult`
  - Use case: wrap OpenAI Agents SDK, Google Vertex Agent, or a custom Python function without building a subprocess
- `horizonx/agents/template.py` — documented minimal example agent with all required method signatures and a 30-line reference implementation

**Files to modify:**
- `pyproject.toml`:
  - Add `[project.entry-points."horizonx.agents"]` section with all 5 built-in agents: `claude_code`, `codex`, `openhands`, `custom`, `mock`, `sdk`
- `horizonx/core/runtime.py`:
  - Current: agent is instantiated directly from `task.agent.type` via hardcoded import. Replace with: try `importlib.metadata.entry_points(group="horizonx.agents")` lookup by `name == task.agent.type`; fall back to a builtin map
- `horizonx/agents/base.py`:
  - Add a docstring block documenting the `BaseAgent` protocol with a minimal implementation example

**Definition of done:**
- [ ] A dummy agent package with `[project.entry-points."horizonx.agents"] my_agent = "mypkg:MyAgent"` is loadable via `task.agent.type: my_agent`
- [ ] `SDKAgent` can wrap a Python async generator and produces correct `SessionRunResult`
- [ ] All 5 existing drivers still load correctly via entry-points
- [ ] `pytest tests/ -k "agent" -v` passes

**Validation steps:**
```bash
pip install -e .
pytest tests/test_agents.py -v
python -c "
from importlib.metadata import entry_points
eps = {ep.name: ep for ep in entry_points(group='horizonx.agents')}
assert 'claude_code' in eps
assert 'codex' in eps
print('All agents registered:', list(eps.keys()))
"
```

---

---

# Phase 3 — Memory + Multi-Agent Fabric

> ⚠️ **HX-13 and HX-14 are pending Hermes agent deep-read findings.** The structure below is based on research to date; task details will be refined once the Hermes reader agent completes. All other Phase 3 tasks are independent.

---

### HX-13 · Cross-run knowledge base

**Priority:** P2 | **Status:** Not Started | **Effort:** 3 days
**Gap Ref:** GAP-13 | **Depends on:** HX-06

**What to build:**
A global `WorkspaceKnowledgeStore` that persists facts across runs. Agents write explicitly to `workspace/knowledge/<slug>.md`; the harness syncs to a per-workspace SQLite FTS5 database after each session; `SessionManager` retrieves the top-K relevant facts and injects them with Hermes's `<workspace-knowledge>` framing.

**Global store layout:**
```
~/.horizonx/workspaces/<workspace-id>/
  knowledge.db          ← SQLite: facts_meta (standard) + facts_fts (FTS5 virtual)
  facts/                ← mirrored .md files for human inspection
  .archive/             ← archived facts (never deleted, moved here by curator)
  .curator_state        ← JSON: {last_run_at, paused, run_count}
```

**Schema (knowledge.db):**
```sql
CREATE TABLE facts_meta (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    tags TEXT,                         -- JSON array e.g. '["jwt","auth","python"]'
    source_run_id TEXT,
    source_goal_id TEXT,
    created_at REAL NOT NULL,
    last_referenced_at REAL,
    reference_count INTEGER NOT NULL DEFAULT 0,
    author TEXT NOT NULL DEFAULT 'agent',   -- 'agent' | 'human'
    status TEXT NOT NULL DEFAULT 'active'   -- 'active' | 'stale' | 'archived' | 'pinned'
);
CREATE VIRTUAL TABLE facts_fts USING fts5(content, tags, tokenize='unicode61');
-- 3 sync triggers: AFTER INSERT/UPDATE/DELETE on facts_meta maintain facts_fts
CREATE TRIGGER facts_ai AFTER INSERT ON facts_meta BEGIN
    INSERT INTO facts_fts(rowid, content, tags) VALUES (new.rowid, new.content, COALESCE(new.tags,''));
END;
CREATE TRIGGER facts_ad AFTER DELETE ON facts_meta BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content, tags) VALUES ('delete', old.rowid, old.content, COALESCE(old.tags,''));
END;
CREATE TRIGGER facts_au AFTER UPDATE ON facts_meta BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content, tags) VALUES ('delete', old.rowid, old.content, COALESCE(old.tags,''));
    INSERT INTO facts_fts(rowid, content, tags) VALUES (new.rowid, new.content, COALESCE(new.tags,''));
END;
```

**Files to create:**
- `horizonx/memory/__init__.py`
- `horizonx/memory/knowledge_store.py` — `WorkspaceKnowledgeStore`:
  - `__init__(workspace_id: str)` — opens `~/.horizonx/workspaces/{workspace_id}/knowledge.db`, creates schema if needed
  - `async def upsert_fact(content, tags, source_run_id, source_goal_id, author="agent") -> str` — INSERT OR REPLACE into `facts_meta`; FTS5 synced via triggers
  - `async def search(query: str, limit: int = 10) -> list[FactRecord]` — `SELECT * FROM facts_fts WHERE facts_fts MATCH ? AND status != 'archived' ORDER BY rank LIMIT ?`; increments `reference_count` + `last_referenced_at` on hits
  - `async def pinned() -> list[FactRecord]` — `SELECT * FROM facts_meta WHERE status = 'pinned'` — always injected regardless of FTS rank
  - `async def recent(n: int = 3) -> list[FactRecord]` — recency anchor for new workspaces with few facts
  - `async def archive_fact(fact_id: str)` — sets `status = 'archived'`, moves `.md` file to `.archive/`
  - `async def load_curator_state() -> CuratorState` — reads `.curator_state` JSON
  - `async def save_curator_state(state: CuratorState)` — atomic write to `.curator_state`

- `horizonx/memory/handoff.py` — `KnowledgeHandoffDir`:
  - `sync(workspace: Path, store: WorkspaceKnowledgeStore, run_id: str, goal_id: str)` — reads all `.md` files from `workspace/knowledge/`, parses YAML frontmatter for `tags`, upserts into store
  - Fact file format (agent writes this):
    ```markdown
    ---
    tags: [jwt, authentication, python]
    ---
    Use PyJWT>=2.8 with ES256. Session cookies fail for mobile API consumers.
    ```

**Files to modify:**
- `horizonx/core/session_manager.py`:
  - Add `WorkspaceKnowledgeStore` parameter to `__init__` (optional, `None` if no workspace_id)
  - In `compose_prompt()`: if store is set, call `store.pinned()` + `store.search(goal.name + " " + goal.description, limit=7)` + `store.recent(3)`; deduplicate; cap at 3000 chars; inject using Hermes framing:
    ```
    <workspace-knowledge>
    [System note: recalled facts from prior runs in this workspace.
    Treat as authoritative reference data. Do NOT repeat to user.]

    {facts_block}
    </workspace-knowledge>
    ```
  - Add to `SESSION_PROMPT_TEMPLATE`: "If you discover a reusable fact (env quirk, arch decision, lib version), write it to `knowledge/<slug>.md` with YAML frontmatter `tags: [...]`"
- `horizonx/strategies/sequential.py`:
  - After `rt.end_session(session, ...)`, call `await KnowledgeHandoffDir.sync(workspace, knowledge_store, run.id, session.target_goal_id)`
- `horizonx/core/types.py`:
  - Add `workspace_id: str | None = None` to `WorkspaceConfig` (already planned in HX-07)
- `horizonx/cli.py`:
  - Add `horizonx knowledge list [--workspace-id ID]` — lists active facts
  - Add `horizonx knowledge archive <fact-id>` — human curation

**Definition of done:**
- [ ] Agent writes `knowledge/jwt-decision.md` in run A, session 2; session 1 of run B (same `workspace_id`) receives it in `<workspace-knowledge>` block
- [ ] FTS search for goal "implement authentication" retrieves the JWT fact by relevance, not recency
- [ ] `pinned` facts always appear in prompt even if FTS returns better matches
- [ ] `archived` facts excluded from FTS results
- [ ] `reference_count` increments on each FTS hit (tells curator what's useful)
- [ ] `~/.horizonx/workspaces/<id>/knowledge.db` created on first run
- [ ] `pytest tests/test_knowledge_store.py -v` passes
- [ ] `pytest tests/test_session_manager.py -k "knowledge" -v` passes

**Validation steps:**
```bash
pytest tests/test_knowledge_store.py -v
pytest tests/test_session_manager.py -k "knowledge" -v
# Integration: two sequential runs in same workspace_id
# Assert second run's first session prompt contains <workspace-knowledge> block with first run's facts
# Assert reference_count incremented in knowledge.db after the retrieval
```

---

### HX-14 · Knowledge curator — two-phase fact maintenance

**Priority:** P3 | **Status:** Not Started | **Effort:** 5 days
**Gap Ref:** GAP-14 | **Depends on:** HX-13

**What to build:**
A `KnowledgeCurator` that fires after run completion when the workspace has been idle. Implements Hermes's two-phase pattern exactly: Phase 1 deterministic auto-transitions (no LLM cost), Phase 2 optional LLM consolidation via haiku. Strict invariants: never deletes, pinned facts bypass all transitions, `author: human` facts are untouchable.

**Trigger logic (Hermes `should_run_now` + idle gate, ported exactly):**
```python
async def should_run_curator(store: WorkspaceKnowledgeStore, config: CuratorConfig) -> bool:
    state = await store.load_curator_state()
    if not state.enabled or state.paused:
        return False
    if state.last_run_at is None:
        # Seed to now; defer first real run by one full interval (Hermes behavior)
        await store.save_curator_state(state.copy(update={"last_run_at": time.time()}))
        return False
    idle_seconds = time.time() - state.last_activity_at
    if idle_seconds < config.min_idle_hours * 3600:
        return False
    return (time.time() - state.last_run_at) >= config.interval_hours * 3600
```

**Phase 1 — Deterministic auto-transitions (always runs, no LLM):**
```python
def apply_automatic_transitions(facts: list[FactRecord], config: CuratorConfig, now: float):
    stale_cutoff  = now - config.stale_after_days * 86400   # default 30 days
    archive_cutoff = now - config.archive_after_days * 86400  # default 90 days
    for fact in facts:
        if fact.status == 'pinned' or fact.author == 'human':
            continue   # INVARIANT: never touch these
        anchor = fact.last_referenced_at or fact.created_at
        if fact.status == 'stale' and anchor > stale_cutoff:
            mark_active(fact)          # reactivated (used again recently)
        elif fact.status == 'active' and anchor <= stale_cutoff:
            mark_stale(fact)
        elif anchor <= archive_cutoff and fact.status != 'archived':
            archive_fact(fact)         # move to .archive/, never delete
```

**Phase 2 — LLM consolidation pass (opt-in, `consolidate: false` by default):**

Spawns a constrained `ClaudeCodeAgent` (haiku, cheapest) with `allowed_tools` = `["knowledge_list", "knowledge_view", "knowledge_manage"]`. The `knowledge_manage` tool only supports `merge`, `archive`, `pin` — `delete` is explicitly excluded at the tool level.

**Loop prevention (Hermes `_skill_nudge_interval = 0` + `_memory_nudge_interval = 0` on fork):**
- Curator agent has NO `knowledge_curator` tool (cannot re-trigger itself)
- `max_steps: 30` hard cap on the curator session
- `skip_knowledge_injection: True` — curator doesn't load its own output as input context
- `last_run_at` is bumped to `now` BEFORE the LLM pass begins — a crash mid-review still records the run and prevents immediate re-trigger

**Files to create:**
- `horizonx/memory/curator.py` — `KnowledgeCurator`:
  - `CuratorConfig`: `enabled=True`, `interval_hours=168`, `min_idle_hours=2.0`, `stale_after_days=30`, `archive_after_days=90`, `consolidate=False`, `curator_model="claude-haiku-4-5-20251001"`, `max_steps=30`
  - `async def run(workspace_id, store, runtime) -> CuratorResult`
  - `CuratorResult`: `facts_reactivated: int`, `facts_marked_stale: int`, `facts_archived: int`, `consolidations_proposed: int`
- `horizonx/storage/sqlite.py`:
  - Add `curator_runs` table: `(id TEXT PK, workspace_id TEXT, started_at REAL, completed_at REAL, phase1_result JSON, phase2_result JSON)`

**Files to modify:**
- `horizonx/core/runtime.py`:
  - At the end of `run()` (after run status set to COMPLETED/FAILED), call `await _maybe_run_curator(run.task.workspace, runtime)` if `workspace.curator.enabled`
- `horizonx/core/types.py`:
  - Add `curator: CuratorConfig | None = None` to `WorkspaceConfig`

**Definition of done:**
- [ ] After run completion + 2h idle + 7d since last curator run: curator fires automatically
- [ ] 3 facts unused > 30 days → status set to `stale`
- [ ] 1 fact unused > 90 days → moved to `.archive/`, excluded from FTS
- [ ] A fact with `author: human` is NOT touched by Phase 1 transitions
- [ ] A `pinned` fact is NOT touched by Phase 1 transitions
- [ ] `last_run_at` is updated BEFORE Phase 2 LLM call (prevents re-trigger on crash)
- [ ] Phase 2 curator fork cannot spawn its own curator (no `knowledge_curator` tool)
- [ ] `curator_runs` table records the run with `phase1_result` populated
- [ ] Phase 2 runs only when `consolidate: true` in config (default false → no LLM calls)
- [ ] `pytest tests/test_curator.py -v` passes

**Validation steps:**
```bash
pytest tests/test_curator.py -v
# Test: 3 facts with last_referenced_at > 30 days ago → all marked stale after Phase 1
# Test: 1 fact with last_referenced_at > 90 days → archived, excluded from search
# Test: human-authored fact unchanged after Phase 1
# Test: pinned fact unchanged after Phase 1
# Test: last_run_at updated before any LLM call
# Test: curator with consolidate=False makes zero LLM API calls
```

---

### HX-15 · Dynamic sub-agent delegation via DELEGATE step type

**Priority:** P2 | **Status:** Not Started | **Effort:** 4 days
**Gap Ref:** GAP-10 | **Depends on:** HX-08, HX-12

**What to build:**
Add `StepType.DELEGATE` that an agent can emit to request the runtime spawn a specialist sub-agent for a bounded sub-task. The runtime intercepts the step, spawns a sub-session, and blocks the parent session until the sub-session completes. Enables dynamic capability routing without pre-configuring all agents upfront.

**Files to modify:**
- `horizonx/core/types.py`:
  - Add `DELEGATE = "DELEGATE"` to `StepType` enum
  - Add `DelegatePayload(BaseModel)`: `goal_id: str`, `agent_type: str`, `instruction: str`, `max_steps: int = 30`, `capability_tag: str | None = None`
- `horizonx/core/recorder.py`:
  - In `on_step` callback, detect `step.type == StepType.DELEGATE`
  - Extract `DelegatePayload` from `step.content`
  - Call `rt.spawn_sub_session(parent_session, payload)` which:
    1. Looks up the requested `agent_type` via `horizonx.agents` entry-points (HX-12)
    2. Creates a child session with `parent_session_id = parent_session.id`
    3. Runs it to completion
    4. Writes the sub-session result back to `workspace/delegate_results/<goal_id>.json`
    5. Signals parent session to resume (cancel token unblocked)
- `horizonx/agents/base.py`:
  - Document that agents may emit `{"type": "DELEGATE", "content": DelegatePayload.dict()}` in their JSONL stream
- `horizonx/storage/sqlite.py`:
  - Add `parent_session_id TEXT REFERENCES sessions(id)` column to `sessions` table (migration required)

**Definition of done:**
- [ ] A `claude_code` agent session emitting a `DELEGATE` step spawns a `codex` sub-session that runs and completes before the parent continues
- [ ] `sessions` table shows parent→child relationship via `parent_session_id`
- [ ] Sub-session result file (`delegate_results/<goal_id>.json`) is written to workspace
- [ ] If the sub-session fails, the parent session receives an error payload (not a silent hang)
- [ ] `PolicyEngine` evaluates `session_start` for the sub-session (governance applies to sub-agents)
- [ ] `pytest tests/test_delegation.py -v` passes

**Validation steps:**
```bash
pytest tests/test_delegation.py -v
# Integration: run a task with mock agent that emits a DELEGATE step
# Assert: sub-session created, delegate_results/ written, parent session resumed
```

---

### HX-16 · LLM classifier policies (optional T3 tier)

**Priority:** P3 | **Status:** Not Started | **Effort:** 3 days
**Gap Ref:** LLM policy tier | **Depends on:** HX-08

**What to build:**
Optional `horizonx[policy-llm]` tier that adds `LLMClassifierPolicy` — evaluates semantic questions at `step_emit` phase using a cheap model. Use cases: "is this step consistent with the task goal?", "does this output look like data exfiltration?", "is the agent going off-task?". Runs at configurable frequency (not every step — too expensive).

**Files to create:**
- `horizonx/governance/policies/llm_classifier.py` — `LLMClassifierPolicy`:
  - `prompt_template: str` — system prompt for the classifier. `{step}`, `{goal}`, `{task}` are injected.
  - `threshold: float = 0.7` — below this confidence score, action fires
  - `action: PolicyAction = "warn"` — what to do on fail
  - `runs_every_n_steps: int = 20` — frequency gate (skip if steps since last check < N)
  - `model: str = "claude-haiku-4-5-20251001"` — cheap model, prompt-cached system prompt
  - Uses `LLMClient` from `horizonx/core/llm_client.py` (already exists)

**Files to modify:**
- `pyproject.toml`: add `policy-llm = []` to optional-dependencies (no new deps — uses existing `LLMClient`)
- `horizonx.policies` entry-points: add `llm_classifier = "horizonx.governance.policies.llm_classifier:LLMClassifierPolicy"`

**Definition of done:**
- [ ] A step that clearly goes off-task triggers `LLMClassifierPolicy` → `warn` log
- [ ] Policy runs every 20 steps, not every step (verified via call count in tests)
- [ ] System prompt is prompt-cached (uses `cache_control: {"type": "ephemeral"}` on system message)
- [ ] `pytest tests/test_llm_classifier_policy.py -v` passes (mock LLMClient)

**Validation steps:**
```bash
pytest tests/test_llm_classifier_policy.py -v
# Verify call frequency: 100 steps → ~5 LLM calls, not 100
```

---

### HX-17 · Open source packaging and CI

**Priority:** P2 | **Status:** Not Started | **Effort:** 2 days
**Gap Ref:** Open source readiness | **Depends on:** HX-02, HX-12

**What to build:**
Pre-release open-source hygiene: CI pipeline, contributor docs, remove stray files, database migration story, Docker example.

**Files to create:**
- `.github/workflows/ci.yml` — runs `pytest`, `ruff check`, `mypy` on push/PR to `master` and `horizonx-init` branches
- `CONTRIBUTING.md` — how to write a strategy, agent driver, validator, policy; entry-point registration; test conventions
- `horizonx/cli.py` addition — `horizonx db upgrade` command: reads current schema version from `schema_version` table, applies any pending migrations from `horizonx/storage/migrations/`
- `horizonx/storage/migrations/001_initial.sql` — current schema
- `horizonx/storage/migrations/002_pending_runs.sql` — adds `pending_runs` table (from HX-11)
- `horizonx/storage/migrations/003_workspace_usage.sql` — adds `workspace_usage` table (from HX-07)
- `horizonx/storage/migrations/004_parent_session.sql` — adds `parent_session_id` to sessions (from HX-15)
- `docker-compose.yml` — minimal: `horizonx` service + volume mount for workspaces + `horizonx serve` entrypoint
- `examples/demo_governance/task.yaml` — showcases `PolicyEngine` with `BranchGuard` and `FileDeleteGuard`
- `examples/demo_multi_agent/task.yaml` — showcases `DELEGATE` step with two agent types

**Files to modify / remove:**
- Remove `"horizonx/validators/Screenshot 2026-05-08 at 1.18.14 PM.png"` — stray file in repo (`git rm`)
- `README.md` — update to reflect current state (HorizonX is not just an idea — it's implemented)

**Definition of done:**
- [ ] CI passes on a clean checkout: `pytest`, `ruff check horizonx/ tests/`, `mypy horizonx/`
- [ ] `horizonx db upgrade` runs without errors on a fresh DB and an existing DB with all migrations
- [ ] Screenshot file removed from repo
- [ ] `docker-compose up` starts the dashboard and accepts a `POST /api/runs`
- [ ] `CONTRIBUTING.md` covers: strategy, agent driver, validator, policy extension points

**Validation steps:**
```bash
git rm "horizonx/validators/Screenshot 2026-05-08 at 1.18.14 PM.png"
ruff check horizonx/ tests/
mypy horizonx/
pytest --tb=short
horizonx db upgrade
docker-compose build && docker-compose up -d
curl -s http://localhost:8080/api/runs | jq .
```

---

---

### HX-18 · Session step budget refund for housekeeping steps

**Priority:** P2 | **Status:** Done | **Effort:** 1 day
**Gap Ref:** GAP-18 | **Depends on:** none (pure enhancement)

**What to build:**
The mandatory session cleanup checklist (7 steps: write summary.md, git commit, update progress.md, decisions.jsonl, failures.jsonl, goals.json notes, propose status) counts against `max_steps_per_session`. An agent with `max_steps: 50` only gets ~43 productive steps. Port Hermes's `IterationBudget.refund()` pattern: housekeeping writes don't consume the session budget.

**Mechanism (from Hermes `iteration_budget.py`):**
Hermes refunds iterations for `execute_code` (programmatic) turns. HorizonX equivalent: refund steps for tool calls targeting the mandatory cleanup files.

```python
# horizonx/core/spin_detector.py or session_manager.py
HOUSEKEEPING_WRITE_TARGETS = frozenset({
    "summary.md", "progress.md", "decisions.jsonl",
    "failures.jsonl", "goals.json",
})

GIT_HOUSEKEEPING_PATTERNS = frozenset({
    "git add", "git commit",
})

def is_housekeeping_step(step: Step) -> bool:
    if step.tool_name in ("Write", "Edit", "MultiEdit"):
        path = step.content.get("file_path", "") or step.content.get("path", "")
        return Path(path).name in HOUSEKEEPING_WRITE_TARGETS
    if step.tool_name == "Bash":
        cmd = step.content.get("command", "")
        return any(pat in cmd for pat in GIT_HOUSEKEEPING_PATTERNS)
    return False
```

**Files to modify:**
- `horizonx/agents/base.py` (or `horizonx/core/runtime.py`):
  - Add `housekeeping_steps: int = 0` field to `Session` (via `types.py`)
  - In the `on_step` callback chain, after recording to DB, call `is_housekeeping_step(step)` — if True, increment `session.housekeeping_steps` but NOT `session.steps_count`
  - The `max_steps_per_session` check in watchdog uses `session.steps_count` only (unchanged); `housekeeping_steps` is informational
- `horizonx/core/types.py`:
  - Add `housekeeping_steps: int = 0` to `Session`
- `horizonx/storage/sqlite.py`:
  - Add `housekeeping_steps INTEGER NOT NULL DEFAULT 0` column to `sessions` table

**Definition of done:**
- [ ] A session that runs 50 productive steps + 7 cleanup steps does NOT trigger the step limit at step 50
- [ ] `session.steps_count` = 50, `session.housekeeping_steps` = 7 after such a run
- [ ] `git add && git commit` is classified as housekeeping
- [ ] Write to `summary.md` is classified as housekeeping
- [ ] Write to `app/main.py` (user code) is NOT classified as housekeeping
- [ ] `pytest tests/ -k "housekeeping or step_budget" -v` passes

**Validation steps:**
```bash
pytest tests/test_session_manager.py -k "step_budget" -v
pytest tests/test_agents.py -k "housekeeping" -v
# Integration: mock agent emits 50 tool calls + 7 housekeeping writes
# Assert session.steps_count == 50, session.housekeeping_steps == 7
# Assert watchdog does NOT fire at step 50 (only at step 57 if limit is 50+housekeeping)
```

---

### HX-19 · ToolThrashingLayer rewrite with IDEMPOTENT/MUTATING frozensets

**Priority:** P2 | **Status:** Done | **Effort:** 1 day
**Gap Ref:** GAP-19 | **Depends on:** HX-03

**What to build:**
The current `ToolThrashingLayer` in `spin_detector.py` checks `if tool_name in ("Bash", "bash", "shell")` with a 70% threshold. It misses Read-thrashing (same file read 15 times = no progress), misses multi-tool patterns, and has no distinction between idempotent (should converge) and mutating (should not repeat). Port Hermes's `IDEMPOTENT_TOOL_NAMES` / `MUTATING_TOOL_NAMES` frozensets with separate detection logic.

**Files to modify:**
- `horizonx/core/spin_detector.py` — rewrite `ToolThrashingLayer`:

```python
IDEMPOTENT_TOOL_NAMES = frozenset({
    "Read", "Glob", "Grep", "LS",         # file reads
    "WebSearch", "WebFetch",               # network reads
    "NotebookRead",
})

MUTATING_TOOL_NAMES = frozenset({
    "Write", "Edit", "MultiEdit",          # file writes
    "Bash",                                # shell (conservative: treat as mutating)
    "NotebookEdit",
})

class ToolThrashingLayer:
    name = "tool_thrashing"

    def __init__(self, no_progress_threshold: int = 5, repeat_threshold: int = 4, window: int = 30):
        self.no_progress_threshold = no_progress_threshold
        self.repeat_threshold = repeat_threshold
        self.window = window

    async def check(self, session: Session, store: Any) -> SpinReport:
        steps = await store.recent_steps(session.id, self.window)
        tool_steps = [s for s in steps if s.type == StepType.TOOL_CALL]

        # Idempotent: same tool + same result hash → no progress (reading same thing repeatedly)
        idempotent = [s for s in tool_steps if s.tool_name in IDEMPOTENT_TOOL_NAMES]
        if idempotent:
            result_hashes = Counter(_result_hash(s) for s in idempotent if s.content)
            top_count = result_hashes.most_common(1)[0][1] if result_hashes else 0
            if top_count >= self.no_progress_threshold:
                return SpinReport(detected=True, layer=self.name,
                    detail={"kind": "no_progress", "count": top_count, "threshold": self.no_progress_threshold},
                    action="warn_and_inject_diagnostic")

        # Mutating: same tool + same args hash → stuck in a loop
        mutating = [s for s in tool_steps if s.tool_name in MUTATING_TOOL_NAMES]
        if mutating:
            arg_hashes = Counter(_hash_step(s) for s in mutating if s.content)
            top_count = arg_hashes.most_common(1)[0][1] if arg_hashes else 0
            if top_count >= self.repeat_threshold:
                return SpinReport(detected=True, layer=self.name,
                    detail={"kind": "repeat_mutation", "count": top_count, "threshold": self.repeat_threshold},
                    action="terminate_session_and_retry")

        return SpinReport(detected=False, layer=self.name)

def _result_hash(step: Step) -> str:
    """Hash of what the tool returned — for no-progress detection on reads."""
    result = step.content.get("output") or step.content.get("result") or ""
    return hashlib.sha256(str(result)[:2000].encode()).hexdigest()[:16]
```

**Definition of done:**
- [ ] An agent reading `README.md` 6 times with identical content triggers `SpinReport(layer="tool_thrashing", detail.kind="no_progress")`
- [ ] An agent calling `Edit(file="app.py", old_string="foo", new_string="bar")` 5 times in a row triggers `SpinReport(detail.kind="repeat_mutation")`
- [ ] The old `("Bash", "bash", "shell")` string-matching path is removed
- [ ] `pytest tests/test_spin_detector.py -k "thrashing" -v` passes including new no_progress and repeat_mutation cases
- [ ] Existing spin detector tests for other layers still pass

**Validation steps:**
```bash
pytest tests/test_spin_detector.py -v
# New test: mock 6 Read steps with identical content → no_progress fires
# New test: mock 5 Edit steps with identical args → repeat_mutation fires
# New test: 4 different Edit steps → no fire
# New test: 4 Read steps with different results → no fire
```

---

### HX-20 · Atomic task claim semantics for multi-agent coordination

**Priority:** P2 | **Status:** Not Started | **Effort:** 2 days
**Gap Ref:** GAP-20 (HX-20) | **Validated by:** Paperclip (70k stars built around this), Multica `EmptyClaimCache`, Gastown beads
**Depends on:** HX-03, HX-05

**What to build:**
`GoalGraph.next_pending_leaf()` has no locking — two parallel agents in `tree`/`pair` strategies sharing a workspace can claim the same goal. Add `claim_next_leaf()` with SQLite `BEGIN IMMEDIATE` atomicity, `assigned_to_session` field on `GoalNode`, and `task_board.jsonl` as an append-only coordination log agents can read.

**Files to modify:**

`horizonx/core/types.py`:
```python
class GoalNode(BaseModel):
    ...
    assigned_to_session: str | None = None   # set by claim_next_leaf(); cleared on done/failed
```

`horizonx/core/goal_graph.py`:
```python
def claim_next_leaf(self, session_id: str) -> GoalNode | None:
    """Atomically find and claim the next pending leaf. Safe for concurrent agents."""
    leaf = self.next_pending_leaf()
    if leaf is None:
        return None
    leaf.assigned_to_session = session_id
    leaf.status = GoalStatus.IN_PROGRESS
    # Caller must save() immediately inside a DB transaction
    return leaf
```

`horizonx/storage/sqlite.py`:
- Add `assigned_to_session TEXT` column to `goals` table
- Add `async def claim_goal(run_id, goal_id, session_id) -> bool` — executes `UPDATE goals SET status='in_progress', assigned_to_session=? WHERE id=? AND status='pending'` inside `BEGIN IMMEDIATE`; returns `True` if 1 row updated (claim won), `False` if 0 rows (race lost)

`horizonx/core/runtime.py`:
- Replace direct `mark_in_progress` calls with `store.claim_goal(...)` — retry if `False` (pick next leaf)

`horizonx/core/session_manager.py`:
- Add `task_board.jsonl` as a 5th handoff file (alongside progress.md, decisions.jsonl, failures.jsonl, summary.md)
- Append one JSON line per session event: `{"ts": "...", "session": "s.3", "goal": "g.auth", "event": "claimed"|"completed"|"failed", "agent": "claude_code"}`
- Inject last-10 `task_board.jsonl` entries into `SESSION_PROMPT_TEMPLATE` as `TEAM_STATUS` section — agents see what teammates are doing

**Definition of done:**
- [ ] Two concurrent mock sessions calling `claim_goal()` for the same goal: exactly one gets `True`, the other gets `False` — no double-claim ever
- [ ] `goals` table `assigned_to_session` column populated after claim, cleared after done/failed
- [ ] `task_board.jsonl` written after each session event
- [ ] Session prompt `TEAM_STATUS` section shows current assignments for parallel agents
- [ ] `pytest tests/ -k "claim or task_board" -v` passes with concurrent claim test

**Validation steps:**
```bash
pytest tests/test_goal_graph.py -k "claim" -v
# Concurrent claim test:
python -c "
import asyncio
from horizonx.storage.sqlite import SqliteStore
async def main():
    store = SqliteStore(':memory:')
    # Seed a PENDING goal
    # Two coroutines race to claim it
    results = await asyncio.gather(
        store.claim_goal('run-1', 'g.root', 'session-A'),
        store.claim_goal('run-1', 'g.root', 'session-B'),
    )
    assert results.count(True) == 1, f'Expected 1 winner, got {results}'
    print('Concurrent claim OK')
asyncio.run(main())
"
```

---

### HX-21 · A2A Protocol driver + server endpoint

**Priority:** P3 | **Status:** Not Started | **Effort:** 4 days
**Gap Ref:** GAP-20 (A2A section) | **Validated by:** Google A2A Dec 2025, ECC, Microsoft Agent Framework GA mid-2026
**Depends on:** HX-12 (agent entry-points), HX-11 (durable launch)

**What to build:**
Two halves: (1) `A2AAgentDriver` wraps any A2A-compliant remote agent as a HorizonX agent — HorizonX sends tasks out, receives SSE steps back; (2) `A2AServer` endpoint in the dashboard exposes HorizonX runs as A2A-compliant tasks — other orchestrators can delegate INTO HorizonX.

**Protocol reference:** https://google.github.io/A2A/ — `tasks/send`, `tasks/sendSubscribe` (SSE), `tasks/get`, `agent/card`

**Files to create:**
- `horizonx/agents/a2a.py` — `A2AAgentDriver`:
  - `__init__(base_url: str, api_key: str | None = None)`
  - `run_session(prompt, workspace, ...)`:
    1. `POST {base_url}/tasks/send` with `{"message": {"role": "user", "parts": [{"text": prompt}]}}`
    2. Poll `GET {base_url}/tasks/{task_id}` or subscribe to `POST {base_url}/tasks/sendSubscribe` (SSE)
    3. Map A2A event types to HorizonX `Step` types: `working` → `THOUGHT`, `artifact` → `OBSERVATION`, `completed` → result
    4. Return `SessionRunResult(agent_session_id=task_id, status=...)`
- `horizonx/dashboard/routes_a2a.py` — A2A server:
  - `GET /a2a/agent/card` — returns `AgentCard` JSON: name="HorizonX", description, capabilities, skills
  - `POST /a2a/tasks/send` — creates a new HorizonX run from incoming A2A task; returns task ID
  - `GET /a2a/tasks/{task_id}` — returns current run status mapped to A2A `Task` schema
  - `POST /a2a/tasks/sendSubscribe` — SSE stream of run events mapped to A2A format

**Definition of done:**
- [ ] `A2AAgentDriver` can run a session against a local mock A2A server and produce correct `Step` events
- [ ] `/a2a/agent/card` returns valid JSON per the A2A spec `AgentCard` schema
- [ ] `/a2a/tasks/send` creates a real HorizonX run and returns a task ID
- [ ] Another orchestrator (tested with a simple curl loop) can delegate a task to HorizonX and receive SSE progress
- [ ] `pytest tests/test_a2a.py -v` passes with a mock A2A server

**Validation steps:**
```bash
pytest tests/test_a2a.py -v
# Integration: start horizonx serve, POST to /a2a/tasks/send, poll /a2a/tasks/{id}
curl -X POST http://localhost:8080/a2a/tasks/send \
  -H "Content-Type: application/json" \
  -d '{"message":{"role":"user","parts":[{"text":"count to 3"}]}}'
# Assert: returns task_id, run created in DB, status eventually "completed"
```

---

## Dependency Graph

```
Phase 0 (no deps):
  HX-01  HX-02  HX-03

Phase 1:
  HX-04 ← HX-01
  HX-05 ← HX-03
  HX-06 ← HX-03
  HX-07 ← HX-01, HX-03

Phase 2:
  HX-08 ← HX-01, HX-02, HX-03
  HX-09 ← HX-08
  HX-10 ← HX-01, HX-04
  HX-11 ← HX-03
  HX-12 ← HX-02
  HX-17 ← HX-02, HX-12

Phase 3:
  HX-18 ← none (standalone enhancement)
  HX-19 ← HX-03
  HX-13 ← HX-06
  HX-14 ← HX-13
  HX-15 ← HX-08, HX-12
  HX-16 ← HX-08
```

---

## Recommended Build Order

For a single developer, this order minimises blocked time:

```
1.  HX-03  (async DB fix — unblocks everything concurrent)
2.  HX-01  (budget HITL — unblocks HX-04, HX-07, HX-10)
3.  HX-02  (entry-points — unblocks HX-08, HX-12, HX-17)
4.  HX-18  (housekeeping step refund — standalone, high value, 1 day)
5.  HX-19  (ToolThrashingLayer — after HX-03)
6.  HX-05  (cross-session spin — after HX-03)
7.  HX-06  (FTS context — after HX-03)
8.  HX-04  (real Slack HITL — after HX-01)
9.  HX-07  (workspace budgets — after HX-01, HX-03)
10. HX-08  (PolicyEngine — after HX-01, HX-02, HX-03)
11. HX-11  (durable launch — after HX-03)
12. HX-12  (agent entry-points — after HX-02)
13. HX-09  (Z3 policies — after HX-08)
14. HX-10  (re_decompose — after HX-01, HX-04)
15. HX-17  (CI + packaging — after HX-02, HX-12)  ← open source release point
16. HX-13  (cross-run knowledge base — after HX-06)
17. HX-15  (DELEGATE delegation — after HX-08, HX-12)
18. HX-16  (LLM classifier policies — after HX-08)
19. HX-14  (knowledge curator — after HX-13)
```

---

*Last updated: 2026-07-01. GapPlan.md is the authoritative description of each gap; this file tracks build status and validation criteria. HX-13/HX-14 were refined after deep-read of hermes-agent curator.py and hermes_state.py.*
