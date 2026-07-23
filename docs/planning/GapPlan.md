# HorizonX — Validated Gap Plan

*Research base: deep-dive of HorizonX source + omnigent, multica, hermes-agent, gastown codebases + 15-point production pain-point survey (Reddit r/ClaudeCode, r/hermesagent, web 2025-2026).*

---

## What HorizonX already does well — do not break

Before listing gaps, the following are genuine strengths that peer projects lack and should not be refactored away:

| Strength | Evidence | Why it matters |
|---|---|---|
| Crash-safe dual-write | `recorder.py` writes SQLite + JSONL atomically per step | Run survives process kill mid-session |
| Goal graph privilege split | Agent proposes; Runtime accepts only after validators pass | Prevents agents from self-declaring done |
| Dangling tool-call repair | `agents/repair.py` injects synthetic `tool_result` before API resume | Solves the most common Claude Code crash recovery failure |
| 6-layer spin detector | `spin_detector.py` — exact loop, bucketed hash, edit-revert, score plateau, tool-thrashing, semantic progress | Most multi-agent harnesses have zero spin detection |
| 8 strategies covering most topologies | single, sequential, ralph, tree, pair, self_critique, decomposition, monitor | No peer project has this breadth |
| Session resume via native IDs | `agent_session_id` stored; passed as `--resume` to Claude Code / Codex | Resumes native context window, not a cold summary |
| Entry-point–based strategy + agent loading | `runtime.py:362` uses `importlib.metadata.entry_points` | Third-party strategies/agents already work |

---

## Validated Gap Inventory

Each gap is graded on: **user pain** (who feels it and when), **value of fix** (what improves), **effort** (engineering days), **priority**.

---

### GAP-01 · ResourceGovernor has two bugs: charge() never called + HITL not triggered

**Files:** `horizonx/core/governor.py`, `horizonx/agents/claude_code.py`, `horizonx/core/types.py`, `horizonx/strategies/sequential.py`

**Root cause (deeper bug):** `governor.charge()` is never called in any production code path. `SessionRunResult` carried no token/cost data so even if charge() were called, it would have nothing to charge with. The governor was completely disconnected from execution — budgets simply didn't work.

**Secondary bug:** Even with charge() wired, `_check_thresholds()` at 75% only published a bus event. Nothing subscribed to it to pause the run. `_check_thresholds` is synchronous — the HITL callback must use `asyncio.create_task()` (fire-and-forget), not `await`, to avoid blocking.

**Fix applied (both bugs fixed):**
1. Added `tokens_in`, `tokens_out`, `cost_usd` fields to `SessionRunResult`
2. Each agent driver (`claude_code.py`, `codex.py`) now populates these from internal usage tracking
3. Added `rt.charge(result)` method on `Runtime`; `_governor_ref` stores the active governor instance
4. `sequential.py` calls `rt.charge(result)` after each `agent.run_session()` — governor now sees real token data
5. Governor `_check_thresholds` calls `asyncio.create_task(self.hitl_callback(...))` at 75% when `"budget_threshold_75"` in `run.task.hitl.triggers`

**Effort:** 2 days total. **Status: DONE. Priority was P0.**

---

### GAP-02 · Validator registry uses hardcoded if/elif — third-party validators silently ignored

**File:** `horizonx/validators/registry.py:10–36`

**What the code does:**
```python
def build_validator(vc, *, store=None):
    if vc.type == "shell": ...
    if vc.type == "test_suite": ...
    ...
    raise ValueError(f"unknown validator type: {vc.type}")
```
`pyproject.toml:61–67` declares `horizonx.validators` entry-points for all 6 built-in validators. If a user installs a third-party package that registers `horizonx.validators = my_pkg:MyValidator`, `build_validator` raises `ValueError` instead of loading it.

**User pain:** An open-source user writes `pip install horizonx-policy-guard` (a business policy validator). Their `task.yaml` specifies `type: policy_guard`. The run crashes with `unknown validator type: policy_guard`. The entry-point registration in the third-party package's `pyproject.toml` is completely ignored. The ecosystem is broken before it starts.

**Value of fix:** Enables the entire third-party validator ecosystem. Without this, HorizonX cannot grow an open-source plugin community for governance, compliance, or domain-specific validation.

**Fix:**
```python
# registry.py — replace if/elif chain with entry-point lookup
from importlib.metadata import entry_points

def build_validator(vc, *, store=None):
    # 1. Try built-ins (fast path)
    _BUILTIN = {"shell": "horizonx.validators.shell:ShellGate", ...}
    ...
    # 2. Fall back to installed entry-points
    eps = {ep.name: ep for ep in entry_points(group="horizonx.validators")}
    if vc.type in eps:
        cls = eps[vc.type].load()
        return cls(cfg, store=store)
    raise ValueError(...)
```

**Effort:** 0.5 days. **Priority: P0 (blocks open-source ecosystem).**

---

### GAP-03 · HITL Slack notification is a stub that logs to stderr

**File:** `horizonx/hitl/gate.py:62–67`

**What the code does:**
```python
async def _notify_slack(channel, run_id, reason, ctx):
    if not channel:
        return
    # Stub. Real impl: from slack_sdk.web.async_client import AsyncWebClient
    sys.stderr.write(f"[slack] would notify {channel} for {run_id}: {reason}\n")
```
The webhook function (`_notify_webhook`) has `timeout=5.0` and `except Exception: pass` — silent failure, no retry. The decision file is polled every 2 seconds (`while not decision_path.exists(): await asyncio.sleep(2.0)`) — no push, no timeout, no escalation.

**User pain:** An operator runs a 6-hour overnight coding job with `notification_type: slack`. At 2am the agent hits an ambiguous design decision and pauses for HITL. The Slack message never arrives. The job hangs until morning. The team's CI is blocked. The operator assumed they'd be paged; instead, 8 hours of compute time is lost waiting.

**Value of fix:** Transforms HITL from a console-only toy into a production async workflow. Unattended overnight runs become viable. This is the feature that makes autonomous agents trustworthy for teams.

**Fix (scoped — no new dependencies unless opted in):**
1. Real Slack: implement `_notify_slack` using `slack_sdk` (already in `horizonx[slack]` optional dep). Post a Block Kit card with run summary + approve/modify/abort buttons. Add a `/hitl` slash command webhook handler to `dashboard/routes_hitl.py`.
2. Webhook retry: replace `timeout=5.0` with exponential backoff (3 attempts, 5/15/30s).
3. Timeout escalation: if no decision arrives within `cfg.timeout_minutes`, auto-escalate to a secondary channel or auto-approve if `cfg.escalation_action == "approve"`.
4. Dashboard push: when `/api/runs/{id}/hitl POST` is called, write the decision file AND broadcast an SSE event so the waiting coroutine wakes immediately instead of polling.

**Effort:** 3 days. **Priority: P0 (makes async operation viable).**

---

### GAP-04 · SpinDetector is stateless — cross-session drift is invisible

**File:** `horizonx/core/spin_detector.py`, called from `horizonx/core/runtime.py:170`

**What the code does:**
Each `SpinDetector` layer queries `store.recent_steps(session.id, window)` — strictly the *current* session. A run where every session writes 3 lines to `output.py`, commits, then exits cleanly will never trigger any spin layer, even after 40 such sessions that collectively made zero progress toward the root goal.

**User pain:** A developer runs a "refactor codebase" task. The sequential strategy spawns sessions. Each session picks a sub-goal, makes a superficially plausible change, passes the `shell` validator (exit code 0), and exits. After session 8 — two days in — the developer notices the code is no different than when they started. The agent has been cycling through surface-level changes. Total waste: 40 API sessions, ~$80 in tokens.

**Value of fix:** Cross-session spin detection catches the pattern every production team hits eventually: the agent that looks productive but makes no durable progress. HorizonX already stores all sessions and steps — this is a query, not new infrastructure.

**Fix:**
Add a `CrossSessionSpinLayer` that runs in `Runtime.check_spin` after the in-session layers. It queries:
```sql
SELECT COUNT(DISTINCT session_id), COUNT(*) FROM validations
WHERE run_id = ? AND score IS NOT NULL
ORDER BY created_at DESC LIMIT 10
```
If the last N validator scores are non-null and their variance is below `plateau_variance` (configurable), and the goal graph shows no status transitions in the last N sessions, fire `SpinReport`. Persist the detector's session-count state in the `spin_reports` table (already exists) so it accumulates across calls.

**Effort:** 2 days. **Priority: P1.**

---

### GAP-05 · SQLite store is sync-blocking inside async methods

**File:** `horizonx/storage/sqlite.py:139`

**What the code does:**
```python
async def save_run(self, run: Run) -> None:
    with sqlite3.connect(self.db_path) as conn:  # ← synchronous, blocks event loop
        ...
```
Every call to `save_run`, `save_step`, `list_runs`, etc. opens a real `sqlite3` connection synchronously inside an `async def`. Under a `tree` strategy with 4 parallel branches, all four coroutines block the single-threaded asyncio event loop on every DB write.

**User pain:** A user runs `tree` strategy with `width: 4` (four parallel agent branches). All four agent subprocess stdout streams need to be processed simultaneously. Instead, processing stalls whenever any branch writes a step. Wall-clock for a 4-branch run is close to sequential, defeating the purpose of parallel branching.

**Value of fix:** Correct concurrency under the `tree`, `pair`, and future multi-agent strategies. Already on CLAUDE.md's planned list. The fix is mechanical — wrap each DB call with `asyncio.get_event_loop().run_in_executor(None, ...)` or switch the write path to use a single dedicated DB thread via `asyncio.Queue`.

**Fix:** Add a `_db_executor = ThreadPoolExecutor(max_workers=1)` to `SqliteStore.__init__`. Wrap each `sqlite3` call with `await asyncio.get_event_loop().run_in_executor(self._db_executor, lambda: ...)`. No schema change.

**Effort:** 2 days. **Priority: P1.**

---

### GAP-06 · Context injection is blind — cold-start uses recency, not relevance

**File:** `horizonx/core/session_manager.py:89–95`

**What the code does:**
```python
decisions_tail=self._tail_jsonl("decisions.jsonl", 20),  # last 20, always
failures_for_goal=self._failures_for_goal(target_goal.id),  # all failures for this goal
progress_tail=self._tail("progress.md", 80),  # last 80 lines, always
```
Context injected at every session start is: the last 80 lines of `progress.md`, the last 20 `decisions.jsonl` entries, and ALL failure entries for the current goal. On a long run (50+ sessions), the last 20 decisions may be 3 days old and completely unrelated to the current goal. The critical decision from session 2 ("we chose postgres not sqlite because...") is gone. The agent cold-starts without it.

**User pain (concrete):** A developer runs a "build REST API with auth" task. Session 3 decided to use JWT tokens (written to `decisions.jsonl` entry 3). By session 25 — now on endpoint validation — that entry is 22 positions back and not injected. The session-25 agent picks up a vague progress.md note and re-implements auth with session tokens, contradicting the earlier decision. The team wastes a session reverting the regression.

**Value of fix:** Relevance-based retrieval turns cold-start from "pray the tail has the right context" into "always load the right context." This is the hermes-agent insight (FTS5 search over session history) adapted for HorizonX's append-only file model. No embeddings needed — FTS on JSONL content is sufficient.

**Fix (scoped — no vector DB):**
1. At run init, create `decisions.db` (SQLite FTS5 virtual table, mirrored from `decisions.jsonl`).
2. In `SessionManager.compose_prompt`, instead of tail-20, query: `SELECT content FROM decisions_fts WHERE decisions_fts MATCH ? ORDER BY rank LIMIT 15` where the query is the current goal's name + description.
3. Keep the last-5-decisions as a recency anchor regardless of relevance score.
4. Cap injected content to 4000 tokens total (count with a simple character/4 estimate); truncate oldest-first if over.

**Effort:** 3 days. **Priority: P1.**

---

### GAP-07 · No governance / business policy layer

**Current state:** HorizonX has `ResourceGovernor` (numeric: tokens/USD/wall-clock). There is no mechanism to evaluate whether a task or tool call is *semantically* permissible: "don't run in production environments," "always require HITL before deleting files," "block tasks tagged `sensitive` from using the `codex` agent," "enforce that all external API calls are logged."

**User pain (two distinct personas):**

*Team lead running agents for junior devs:* Wants to ensure no agent run touches `main` branch without review. Today: impossible to enforce at the harness level. The agent either does it or doesn't based on its prompt.

*Enterprise adopter:* EU AI Act (August 2026 deadline) requires demonstrable human oversight for agents in high-risk categories. Without a governance layer, the audit trail doesn't prove which actions were evaluated against policy.

**Value of fix:** Unlocks team and enterprise use. Makes HorizonX the layer where business rules live, not buried in per-task prompts (which agents can ignore). Omnigent's policy engine is the reference — 3-phase, 3-action — but HorizonX needs a lighter version that fits its run/session/step model.

**Proposed design (NOT Omnigent's full engine — scoped appropriately):**

```
PolicyEngine
├── TaskPolicy     — evaluated at Task intake (before run starts)
├── SessionPolicy  — evaluated before each session starts
└── StepPolicy     — evaluated on emitted Steps (async, non-blocking by default)
```

Each policy is a Python callable (or YAML-declared LLM classifier) returning `PolicyDecision(action: "allow"|"warn"|"block"|"hitl", reason: str)`.

Built-in policies:
- `BranchGuard` — blocks sessions targeting protected branches
- `FileDeleteGuard` — routes file-deletion tool calls to HITL
- `AgentAllowlist` — restricts which agent driver can run for a task tag
- `CostVelocityPolicy` — blocks if tokens/minute exceeds `max_rate` (runaway velocity detection)
- `EnvironmentTagPolicy` — blocks `production`-tagged workspaces from non-HITL-approved tasks

Policies registered via a new `horizonx.policies` entry-point group. `PolicyEngine` loaded in `Runtime.__init__`, called at three points: `runtime.py` before `strategy.execute`, before each `agent.run_session`, and in `recorder.py` after each `on_step`.

**Effort:** 5 days (core engine + 5 built-in policies + YAML declaration). **Priority: P1.**

---

### GAP-08 · No usage policy enforcer — cost velocity and per-workspace quotas unenforceable

**Current state:** `ResourceGovernor` tracks aggregate tokens/USD/wall-clock for a single run. There is no mechanism for:
- Per-workspace daily token budget (org-level)
- Cost velocity detection (tokens/minute rising exponentially = spin loop signal)
- Per-agent-type cost attribution
- Shared quota across concurrent runs (two parallel `tree` branches each think they have the full budget)

**User pain:** A team has 5 developers each running parallel agent sessions. No aggregate view of token burn. One runaway session (a spin loop on a complex refactor) eats the entire team's monthly API budget in 4 hours. The ResourceGovernor for the runaway session thinks it's within its individual `max_total_usd: 20` limit, because no cross-run accounting exists.

**Value of fix:** Makes HorizonX viable for teams. Token budgets at the workspace level are the #1 enterprise requirement. Cost velocity detection catches runaway loops that aggregate budget limits miss (because a loop burns money fast, not much).

**Fix:**
1. Add a `UsageStore` table to SQLite: `(workspace_id, date, tokens_in, tokens_out, usd)` — append-only by run, queryable as a daily rollup.
2. Add `WorkspaceConfig` to `Task`: `workspace_id`, `daily_budget_usd`, `concurrent_run_limit`.
3. At each `governor.charge()` call, also write to `UsageStore` and check the workspace daily total.
4. `CostVelocityPolicy` (from GAP-07 built-ins): samples `tokens_per_minute` as a sliding window over the last 5 `charge()` calls; if rate doubles twice in succession, trigger HITL.

**Effort:** 2 days. **Priority: P1 (complements GAP-07).**

---

### GAP-09 · re_decompose HITL action is declared but never implemented

**File:** `horizonx/strategies/sequential.py:211–212`

**What the code does:**
```python
# re_decompose: not yet implemented — fall through to approve
```
A human operator selecting "re_decompose" at an HITL pause (e.g., "the goal graph is wrong, restructure it") triggers... nothing. The run continues as if they'd selected "approve."

**User pain:** An operator reviewing a stalled agent sees that the goal graph decomposition was wrong — the agent split a goal into 12 micro-steps that are now hopelessly interdependent. They select re_decompose and provide instructions. The run resumes unchanged. They abort and restart from scratch, losing all prior session work.

**Value of fix:** `re_decompose` is the most powerful HITL action — it lets a human course-correct the agent's understanding of the task without throwing away all prior work. The goal graph is already in place; restructuring it is a well-scoped LLM call.

**Fix:**
In `sequential.py`, when `decision.action == "re_decompose"`:
1. Load the current `goals.json`.
2. Call `LLMClient` (already in `Runtime`) with a prompt: "Here is the current goal graph: `{goals_json}`. The operator says: `{decision.instruction}`. Produce a revised `goals.json` that addresses the operator's feedback. Preserve `DONE` goals. Only restructure `PENDING` and `IN_PROGRESS` goals."
3. Validate the result with `GoalGraph.validate()`.
4. Write back to `goals.json` and the `goals` SQLite table.
5. Continue the strategy loop from the new graph's first pending leaf.

**Effort:** 2 days. **Priority: P2 (high value but requires GAP-03 to be useful).**

---

### GAP-10 · No inter-agent messaging — multi-agent coordination is filesystem-only

**Current state:** `pair` strategy coordinates via `guidance.md` (filesystem). `tree` strategy runs branches with zero inter-branch communication. There is no way for Agent A to say "I need a specialist for X" and have the runtime spawn Agent B, hand off a sub-task, and continue.

**User pain:** A developer uses `pair` strategy (Driver + Navigator). The Navigator detects a security issue in the code. It writes `guidance.md` noting this. But the Driver has already moved on and doesn't re-read `guidance.md` mid-session. The security issue ships. If there were a message channel, the Navigator could interrupt the Driver's current session.

**Value of fix (scoped to what's practical):**
Full agent-to-agent message bus (like Omnigent's harness microservices) is overengineered for HorizonX's model. The practical 80% win is:

1. **Structured inter-session handoff messages** — typed `Message` objects (not freeform JSONL) that the next session's `SessionManager.compose_prompt` renders clearly, above the noise of progress.md.
2. **Dynamic sub-task delegation** — an agent can emit a `DELEGATE` step type (new `StepType.DELEGATE`) with a payload `{goal_id, agent_type, instruction}`. The runtime intercepts it, spawns a new session for that goal on the specified agent type, and blocks the parent session until it completes.

This solves the "specialist routing" problem (Agent A hands off a security audit to a security-specialist agent) without a full message bus.

**Effort:** 4 days. **Priority: P2.**

---

### GAP-11 · Dashboard launch is non-durable — process restart loses in-flight runs

**File:** `horizonx/dashboard/routes_launch.py:43`

**What the code does:**
```python
asyncio.create_task(runtime.run(task, run))
```
The run coroutine is an in-memory asyncio Task. If the dashboard process is killed or crashes, the task is gone. The run record in SQLite has status `running` forever (orphaned). There is no way to resume it.

**User pain:** An operator launches a 4-hour task via the dashboard UI. 2 hours in, the server machine is rebooted for security patches. The run status is stuck at `running`. The workspace has partial work. The operator has to manually inspect the workspace, figure out where the run was, and re-launch — losing the in-flight session context.

**Value of fix:** Makes the dashboard a real production launcher, not a demo tool. Crash-resistant run management is a hard requirement for overnight and weekend jobs.

**Fix:**
1. Add a `pending_runs` table: `(run_id, task_json, started_at, status)`.
2. At dashboard startup, query for any runs with `status = running OR status = pending`, and re-attach them via `Runtime.run(task, run)` (run resume is already supported).
3. The `asyncio.create_task` call stays, but is now preceded by a DB write and followed by a status update on exit.

This is not full Temporal-style durability — it's "survive clean process restart," which is the practical 90% case.

**Effort:** 2 days. **Priority: P2.**

---

### GAP-12 · Agent dispatch uses hardcoded if/elif — entry-point agents silently fail + Pydantic blocks third-party types

**Files:** `horizonx/strategies/sequential.py:30–41`, `horizonx/core/types.py:136`

**What the code actually had (two bugs, not one):**
1. `pyproject.toml` already declared `[project.entry-points."horizonx.agents"]` with 4 built-ins — the group existed. But `_build_agent()` in every strategy used a hardcoded `if/elif` chain that never consulted entry-points.
2. `AgentConfig.type` was `Literal["claude_code", "codex", "openhands", "custom", "mock"]` — Pydantic would reject any task YAML specifying a third-party type before entry-point loading was even attempted.

**Fix applied:**
1. Replaced `_build_agent` if/elif with `_BUILTIN_AGENTS` dict fast path + `entry_points(group="horizonx.agents")` plugin path in `sequential.py` (other strategies need the same change — tracked as remaining work)
2. Relaxed `AgentConfig.type` from `Literal[...]` to `str` — third-party types now pass Pydantic validation

**Remaining work:** Apply same `_build_agent` pattern to `pair.py`, `tree.py`, `self_critique.py`, `decomposition.py`, `monitor.py`. Add `sdk` driver for API-based agents. Document `BaseAgent` protocol in `agents/template.py`.

**Effort:** 2 days total (partial fix applied). **Priority: P2.**

---

### GAP-13 · No cross-run knowledge base — facts die with the workspace

**Current state:** `decisions.jsonl` and `failures.jsonl` live inside each run's workspace. When a new run starts — even on the same codebase — there is no mechanism to retrieve facts from prior runs. GAP-06 adds FTS5 search within a run; GAP-13 extends it across runs within a workspace.

**Hermes pattern this ports:** `~/.hermes/skills/` + `.usage.json` provenance + FTS5 search over all sessions. HorizonX's unit of durability is the run workspace; the equivalent global store is `~/.horizonx/workspaces/<workspace-id>/`.

**User pain:** A team runs "add feature" tasks repeatedly on the same codebase. Every run re-discovers: which test runner quirks to avoid, which library version to pin, which architectural decision was made 3 months ago. Each run wastes an early session re-learning facts the harness should remember.

**Fix (scoped — manual write, FTS retrieval, no automatic extraction):**

Global knowledge store layout:
```
~/.horizonx/workspaces/<workspace-id>/
  knowledge.db        ← SQLite with FTS5 + facts_meta table
  facts/              ← agent-written .md files (synced to DB after each session)
  .archive/           ← archived facts (never deleted)
  .curator_state      ← JSON: last_run_at, paused, run_count
```

Schema (`knowledge.db`):
```sql
CREATE TABLE facts_meta (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    tags TEXT,                         -- JSON array
    source_run_id TEXT,
    source_goal_id TEXT,
    created_at REAL NOT NULL,
    last_referenced_at REAL,
    reference_count INTEGER NOT NULL DEFAULT 0,
    author TEXT NOT NULL DEFAULT 'agent',  -- 'agent' | 'human'
    status TEXT NOT NULL DEFAULT 'active'  -- 'active' | 'stale' | 'archived' | 'pinned'
);

CREATE VIRTUAL TABLE facts_fts USING fts5(
    content,
    tags,
    tokenize='unicode61'
);
-- 3 sync triggers: AFTER INSERT/UPDATE/DELETE on facts_meta → maintain facts_fts
```

Session prompt injection (Hermes framing, adapted):
```
<workspace-knowledge>
[System note: The following are recalled facts from previous runs in this workspace.
Treat as authoritative reference data. Do NOT repeat them back to the user.]

{top_k_facts}
</workspace-knowledge>
```

Agent writes facts explicitly to `workspace/knowledge/<slug>.md` with YAML frontmatter:
```markdown
---
tags: [jwt, authentication, python]
---
Use PyJWT>=2.8 with ES256; session cookies don't work for mobile API consumers.
```

After session end, `KnowledgeHandoffDir.sync()` reads all `.md` files and upserts them into `knowledge.db`.

**Effort:** 3 days. **Priority: P2.**

---

### GAP-14 · No skill curator — knowledge base grows stale without maintenance

**Current state:** If GAP-13 is implemented, facts accumulate in `knowledge.db` indefinitely. There is no mechanism to mark stale facts, archive unused ones, merge duplicates, or promote frequently-referenced ones to always-inject status.

**Hermes pattern this ports:** `agent/curator.py` — two-phase: (1) deterministic auto-transitions (no LLM), (2) optional LLM consolidation pass. Key invariants: never auto-deletes; pinned facts bypass transitions; `author: "human"` facts are never touched; curator fork has infinite-loop prevention (`_skill_nudge_interval = 0`).

**Two-phase design:**

**Phase 1 — Deterministic auto-transitions (always, no LLM cost):**
```python
# Mirrors Hermes apply_automatic_transitions()
for fact in facts:
    if fact.status == 'pinned' or fact.author == 'human':
        continue  # invariant: never touch these
    anchor = fact.last_referenced_at or fact.created_at
    if fact.status == 'stale' and anchor > stale_cutoff:   # reactivated
        mark_active(fact)
    elif fact.status == 'active' and anchor <= stale_cutoff:  # going stale
        mark_stale(fact)
    elif anchor <= archive_cutoff:                           # archive
        archive_fact(fact)   # moves to .archive/, never deletes
```

Thresholds: `stale_after_days=30`, `archive_after_days=90` (same as Hermes defaults).

**Phase 2 — LLM consolidation pass (opt-in, `consolidate: false` by default):**

Spawns a constrained `ClaudeCodeAgent` (haiku) with a whitelist of only `knowledge_list`, `knowledge_view`, `knowledge_manage` tools. `knowledge_manage` only supports `merge`, `archive`, `pin` — `delete` is not a valid action. The fork has:
- `max_steps: 30` hard cap
- No access to `knowledge_curator` tool (cannot re-trigger itself — mirrors Hermes `_skill_nudge_interval = 0`)
- `skip_knowledge_injection: True` (doesn't load its own output as context)

**Trigger (mirrors Hermes `should_run_now` + idle gate):**
```python
# Gate 1: enabled + not paused
# Gate 2: last_run_at exists (else seed to now and return False — defers first run by one interval)
# Gate 3: idle_seconds >= min_idle_hours * 3600  (default: 2h)
# Gate 4: (now - last_run_at) >= interval_hours * 3600  (default: 168h / 7 days)
```

Called from `Runtime` at run completion only, not on a background thread.

**Effort:** 5 days. **Priority: P3 (after GAP-13 is running and producing facts).**

---

### GAP-18 · Session step budget counts housekeeping — agents get fewer useful steps

**Current state:** `max_steps_per_session` (default 50) counts every step including the mandatory session cleanup checklist (7 steps: write summary.md, git commit, update progress.md, update decisions.jsonl, update failures.jsonl, update goals.json notes, propose status). An agent with `max_steps: 50` only gets ~43 useful working steps.

**Hermes pattern:** `iteration_budget.py` — `execute_code` turns (programmatic housekeeping) are refunded so they don't burn the user-visible iteration cap. The `refund()` call happens after `execute_code` tool dispatch completes.

**Fix:** Add `HOUSEKEEPING_TOOL_PATTERNS` — a frozenset of `(tool_name, content_pattern)` tuples matching the mandatory cleanup steps. When a step matches, increment a `housekeeping_steps_consumed` counter on the session but don't count against `max_steps`. The session prompt already lists the cleanup checklist explicitly — matching on known patterns is deterministic.

Housekeeping patterns (initial set):
```python
HOUSEKEEPING_WRITES = frozenset({
    "summary.md", "progress.md", "decisions.jsonl", "failures.jsonl", "goals.json"
})
# A Write/Edit step targeting these files → housekeeping
# A Bash step matching "git add" / "git commit" → housekeeping
```

**Effort:** 1 day. **Priority: P2.**

---

### GAP-19 · ToolThrashingLayer uses string matching, not typed tool categories

**Current state:** `spin_detector.py` `ToolThrashingLayer` checks `if tool_name in ("Bash", "bash", "shell")` and fires if Bash is used > 70% of the last 20 steps. This misses thrashing on Read (reading the same file 15 times), misses cross-tool patterns, and uses a magic 70% threshold with no idempotent/mutating distinction.

**Hermes pattern:** Explicit `IDEMPOTENT_TOOL_NAMES` and `MUTATING_TOOL_NAMES` frozensets. Different thresholds for each category. No-progress detection for idempotent tools (same result N times = wasted), repetition detection for mutating tools (same args N times = loop).

**Fix:**
```python
IDEMPOTENT_TOOL_NAMES = frozenset({
    "Read", "Glob", "Grep", "LS",         # File reads
    "WebSearch", "WebFetch",               # Network reads
})

MUTATING_TOOL_NAMES = frozenset({
    "Write", "Edit", "MultiEdit",          # File writes
    "Bash",                                # Shell
    "NotebookEdit",
})

class ToolThrashingLayer:
    async def check(self, session, store):
        steps = await store.recent_steps(session.id, 30)
        tool_steps = [s for s in steps if s.type == StepType.TOOL_CALL]

        # Idempotent: same tool + same content hash → no progress
        idempotent = [s for s in tool_steps if s.tool_name in IDEMPOTENT_TOOL_NAMES]
        result_hashes = Counter(_result_hash(s) for s in idempotent)
        if result_hashes.most_common(1)[0][1] >= self.no_progress_threshold:  # default 5
            return SpinReport(detected=True, layer="tool_thrashing",
                              detail={"kind": "no_progress", ...})

        # Mutating: same tool + same args hash → stuck loop
        mutating = [s for s in tool_steps if s.tool_name in MUTATING_TOOL_NAMES]
        arg_hashes = Counter(_hash_step(s) for s in mutating)
        if arg_hashes.most_common(1)[0][1] >= self.repeat_threshold:  # default 4
            return SpinReport(detected=True, layer="tool_thrashing",
                              detail={"kind": "repeat_mutation", ...})
```

**Effort:** 1 day. **Priority: P2.**

---

## Summary Table — Priority Order

| Gap | File(s) | User Pain | Value | Effort | Priority |
|---|---|---|---|---|---|
| GAP-01 · Budget HITL not triggered | `governor.py:48` | Run hard-crashes at 100% instead of pausing at 75% | Prevents runaway cost loss | 1 day | **P0** |
| GAP-02 · Validator registry if/elif | `validators/registry.py:10` | Third-party validators silently fail | Enables open-source ecosystem | 0.5 day | **P0** |
| GAP-03 · HITL Slack is stderr stub | `hitl/gate.py:62` | Async HITL never delivers notifications | Viable overnight/unattended runs | 3 days | **P0** |
| GAP-04 · Spin detection is per-session | `spin_detector.py`, `runtime.py:170` | Multi-session drift undetected | Catches the "$80 wasted over 8 sessions" pattern | 2 days | **P1** |
| GAP-05 · SQLite sync-blocking | `storage/sqlite.py:139` | Parallel strategies serialize on DB writes | Correct concurrency for tree/pair | 2 days | **P1** |
| GAP-06 · Cold-start uses recency not relevance | `session_manager.py:89` | Critical old decisions not injected | Agent doesn't re-introduce fixed bugs | 3 days | **P1** |
| GAP-07 · No governance/policy layer | (new: `horizonx/governance/`) | No tool-level or task-level policy | Team + enterprise readiness | 5 days | **P1** |
| GAP-08 · No usage policy / velocity | (new: `horizonx/usage/`) | No cross-run quota; runaway loops invisible | Team billing and API quota control | 2 days | **P1** |
| GAP-09 · re_decompose is a no-op | `strategies/sequential.py:211` | Human course-correction silently ignored | Full HITL loop; no wasted restarts | 2 days | **P2** |
| GAP-10 · No inter-agent messaging | (new: `StepType.DELEGATE`) | Agents can't route to specialists | Dynamic specialist delegation | 4 days | **P2** |
| GAP-11 · Dashboard launch non-durable | `routes_launch.py:43` | Server restart orphans in-flight runs | Production launcher viability | 2 days | **P2** |
| GAP-12 · No agent entry-point registration | `pyproject.toml`, `runtime.py` | SDK-based agents need subprocess shims | Universal harness for any agent | 2 days | **P2** |
| GAP-13 · No cross-run knowledge base | (new: `horizonx/memory/`) | Facts re-discovered every run | Agents start smarter from run 2 onwards | 3 days | **P2** |
| GAP-14 · No skill curator | (new: `horizonx/memory/curator.py`) | Knowledge base grows stale + noisy | Maintained, trustworthy global memory | 5 days | **P3** |
| GAP-18 · Housekeeping steps eat session budget | `session_manager.py`, `spin_detector.py` | Agents get 43 useful steps not 50 | Full agent capacity per session | 1 day | **P2** |
| GAP-19 · ToolThrashingLayer uses string matching | `spin_detector.py:ToolThrashingLayer` | Read-thrashing and cross-tool loops undetected | Catches more spin patterns | 1 day | **P2** |

**Total estimated effort: ~38.5 engineering days**

---

## What We Are Explicitly NOT Building

These were considered and rejected as either out of scope, over-engineered, or better handled by the underlying agent CLIs:

| Item | Why not |
|---|---|
| Full Redis event bus | InMemoryBus is fine for single-server; Redis is an optional future backend, not a P1 requirement |
| Embedding/vector store for context | FTS5 SQL search (GAP-06 + GAP-13) solves 90% of the problem without a vector DB dependency |
| Full Omnigent-style 14-harness abstraction | HorizonX's 5 concrete drivers + `custom` covers the cases; entry-points (GAP-12) unlocks the rest |
| Docker sandbox per run | `EnvironmentConfig` type exists; wiring it up is a separate sub-project after infra gaps are fixed |
| LLM-based policy evaluation at every step | T1 Python + T2 Z3 cover most cases; T3 LLM classifiers are P3 optional (`horizonx[policy-llm]`) |
| Distributed multi-Runtime coordination | Requires distributed lock + shared event bus; solve single-server first |
| Hermes cross-platform gateway (Telegram/Discord/WhatsApp) | Out of scope for a coding agent harness; notification target is Slack + webhook only |
| Hermes FTS5 trigram table for CJK | unicode61 tokenizer handles all Latin/English use cases; trigram is a follow-up for international users |
| Multica-style issue board UI | Dashboard is sufficient; issue PM metaphor is a different product |
| Hermes god-object AIAgent pattern | HorizonX's composition-over-inheritance (Runtime + Strategy + Agent) is cleaner; do not consolidate |
| Automatic skill extraction from trajectory | GAP-14 curator does LLM consolidation of *explicitly written* facts; mining raw trajectory is too noisy |

---

## Phased Implementation Roadmap

### Phase 0 — Bug Fixes (2 weeks)
*These are correctness issues. They should ship before any new features.*

1. **GAP-01**: Wire `ResourceGovernor` → `runtime.request_hitl` at 75%. One callback parameter.
2. **GAP-02**: Replace `validators/registry.py` if/elif with `entry_points(group="horizonx.validators")`.
3. **GAP-05**: Wrap `SqliteStore` DB calls with `run_in_executor(self._db_executor, ...)`.

**Deliverable:** HorizonX behaves correctly for concurrent runs and doesn't silently ignore installed validators.

---

### Phase 1 — Production Reliability (4 weeks)
*Makes HorizonX viable for real team use.*

4. **GAP-03**: Real Slack HITL (Block Kit card + slash command), webhook retry, timeout escalation.
5. **GAP-04**: `CrossSessionSpinLayer` querying goal-graph status transitions across sessions.
6. **GAP-06**: FTS5 decision store + relevance-ranked context injection in `SessionManager`.
7. **GAP-08**: `UsageStore` table + workspace daily budget + `CostVelocityPolicy`.

**Deliverable:** Unattended overnight runs work. Cross-session drift is detected. Context is relevant, not random.

---

### Phase 2 — Governance + Open Source Ecosystem (3 weeks)
*Makes HorizonX appropriate for teams and publishable as open source.*

8. **GAP-07**: `PolicyEngine` with `TaskPolicy`, `SessionPolicy`, `StepPolicy` + 5 built-in policies.
9. **GAP-09**: `re_decompose` implementation — LLM-restructures goal graph on HITL instruction.
10. **GAP-11**: Durable dashboard launch — `pending_runs` table + startup recovery.
11. **GAP-12**: `horizonx.agents` entry-point registration + `sdk` driver + `agents/template.py`.

**Deliverable:** An open-source release candidate with a real governance story, ecosystem entry-points, and crash-resistant infrastructure.

---

### Phase 3 — Memory + Multi-Agent Fabric (5 weeks)
*Evolving loop, cross-run knowledge, specialist delegation.*

12. **GAP-18**: Session budget refund for housekeeping steps — `HOUSEKEEPING_WRITES` frozenset, `housekeeping_steps_consumed` counter.
13. **GAP-19**: `ToolThrashingLayer` rewrite with `IDEMPOTENT_TOOL_NAMES` / `MUTATING_TOOL_NAMES` frozensets and separate no-progress / repeat-mutation thresholds.
14. **GAP-13**: Cross-run knowledge base — `WorkspaceKnowledgeStore`, `facts_meta` + `facts_fts` schema, `KnowledgeHandoffDir.sync()`, `<workspace-knowledge>` injection in `SessionManager`.
15. **GAP-10**: `StepType.DELEGATE` + runtime interception + specialist routing.
16. **Squad support**: `squad` config block on `Task`; routing engine matches `capability_tag` to agent type.
17. **GAP-14**: `KnowledgeCurator` — Phase 1 auto-transitions (stale/archive/reactivate) + opt-in Phase 2 LLM consolidation with loop-prevention and pinned-fact invariants.

**Deliverable:** HorizonX has cross-run memory, curator-maintained knowledge, and dynamic specialist delegation.

---

## Cross-Project Insights — What Each Peer Project Validates

| Peer Project | Key Pattern | HorizonX Adoption |
|---|---|---|
| **Omnigent** | Phase-gated policy engine (TOOL_CALL phase is fail-closed) | GAP-07 PolicyEngine borrows the phase model; 3 phases (task_intake, session_start, step_emit) |
| **Multica** | Task-scoped auth tokens + workspace-level budget rollup | GAP-08 UsageStore mirrors Multica's per-workspace accounting |
| **Hermes** | FTS5 cross-session search, two-phase curator, compression lock, iteration budget refund, IDEMPOTENT/MUTATING frozensets | GAP-06 (decisions FTS), GAP-13 (knowledge base), GAP-14 (curator), GAP-18 (budget refund), GAP-19 (thrashing layer) |
| **Gas Town** | Git-backed state as the persistence layer | HorizonX already does this (goals.json + trajectory.jsonl + git commits per session) — validated, no change needed |

### Hermes mechanisms ported (detailed)

| Hermes mechanism | File | HorizonX port |
|---|---|---|
| `build_memory_context_block` — injects into turn, never persisted | `agent/memory_manager.py` | `SessionManager` injects `<workspace-knowledge>` block from FTS search; never written to DB |
| Two-phase curator — deterministic transitions then optional LLM | `agent/curator.py` | `KnowledgeCurator` Phase 1 (stale/archive/reactivate) + Phase 2 (opt-in haiku consolidation) |
| `should_run_now` + idle gate | `agent/curator.py` | `should_run_curator()` — same 4-gate check; seeds `last_run_at` on first call, defers by one interval |
| Infinite loop prevention — `_skill_nudge_interval = 0` on fork | `agent/curator.py` | Curator fork has `no_knowledge_curator` tool restriction; cannot re-trigger |
| Never auto-deletes — only archives to `.archive/` | `agent/curator.py` | `archive_fact()` moves to `.archive/` subdir; `knowledge_manage(delete)` is not a valid tool |
| `compression_lock` — `DELETE expired + INSERT OR IGNORE + SELECT confirm` in `BEGIN IMMEDIATE` | `hermes_state.py` | `CompressionLock` in `horizonx/storage/sqlite.py` for `tree` strategy parallel sessions |
| `IDEMPOTENT_TOOL_NAMES` / `MUTATING_TOOL_NAMES` frozensets | `agent/tool_guardrails.py` | `spin_detector.py` `ToolThrashingLayer` rewrite (GAP-19) |
| `IterationBudget.refund()` for housekeeping turns | `agent/iteration_budget.py` | `HOUSEKEEPING_WRITES` frozenset + `housekeeping_steps_consumed` counter (GAP-18) |
| FTS5 unicode61 + trigram dual tables | `hermes_state.py` | HorizonX uses unicode61 only (GAP-13/GAP-06); trigram deferred for international follow-up |

---

## Open Source Readiness Checklist

Beyond the gaps above, before publishing HorizonX as an open-source project:

- [ ] **CONTRIBUTING.md** — How to write a third-party strategy, agent driver, validator, and policy
- [ ] **examples/** — One working example per strategy type (already have `demo_refactor`, `demo_rest_api`, `demo_word_counter`; add `demo_governance/` and `demo_multi_agent/`)
- [ ] **`horizonx.agents` entry-points** — Match the pattern already used for strategies (GAP-12)
- [ ] **`horizonx.policies` entry-points** — New group for community governance policies (GAP-07)
- [ ] **Remove screenshot from validators/** — `"horizonx/validators/Screenshot 2026-05-08..."` is in the repo
- [ ] **Database migration story** — Currently no version-gated migrations; add `horizonx db upgrade` CLI command wrapping schema diff
- [ ] **Docker example** — Minimal `docker-compose.yml` for running HorizonX + dashboard + test agent
- [ ] **CI**: GitHub Actions with `pytest`, `ruff`, `mypy` — already have the dev deps, just need `.github/workflows/ci.yml`

---

## Ecosystem Landscape — Where HorizonX Fits

*Research across Reddit, GitHub, HackerNews (July 2026) surfaced 20+ active projects. This section maps the landscape and validates or challenges our gaps.*

### Ecosystem map

| System | Stars | Core differentiator | Relevance to HorizonX |
|---|---|---|---|
| **ECC (Everything Claude Code)** | 218k | Cross-harness (7 agents), 5-layer loop prevention, AgentShield | Our entry-point system (GAP-12) + spin detector (HX-19) directly address what they solved |
| **Hermes Agent** | 207k | FTS5 memory, skill curator, 20+ platforms | Deep port basis for GAP-13 + GAP-14 (see Hermes Mechanisms table above) |
| **CrewAI** | 45.9k | Role-based crews + event-driven Flows, 12M daily executions | Validates our strategy pattern; "Flows" = our `composite` strategy |
| **Ruflo** | 59–69k | SONA neural patterns, 12 background workers, Queen swarm, AgentDB vector memory | P4 territory — more complex than our P3 curator; validates long-term memory evolution roadmap |
| **Paperclip** | 70k | Org chart + budget governance, **atomic task checkout** | **Directly validates HX-20** (claim semantics for GoalGraph) — their `#1 user pain` |
| **Multica** | 38.7k | Squad routing, skill compounding, vendor-neutral | In our workspace; validates GAP-08 (workspace budgets) + HX-20 (claim semantics) |
| **Gastown** | 16.1k | Mayor/Polecat role hierarchy, git worktrees, convoy batching | In our workspace; validates goals.json as task board; gap is claim semantics |
| **Hive** | 10.6k | Auto-generates agent topologies, self-healing checkpoint recovery | `re_decompose` (HX-10) is our equivalent but requires HITL; Hive does it automatically |
| **Swarms** | 6.9k | 60+ pre-built swarm architectures, AutoSwarmBuilder | Our 8 strategies cover the same ground more deeply with durability |
| **open-multi-agent** | 6.4k | Goal-first, coordinator builds DAG at runtime, minimal deps | HorizonX has `DecompositionFirst` strategy — validates that approach |
| **ORC** | 19 | Pure bash/tmux/git, 4-tier hierarchy (root/project/goal/engineer), beads | Validates our goal graph depth; beads = our leaf goals; pure bash is the Unix philosophy alternative |
| **Boomerang Tasks (Roo Code)** | — | Parent pauses → subtask runs in specialized mode → parent resumes with summary | **Directly validates HX-15** (DELEGATE step type) — Boomerang is Roo Code's production implementation |

### What this research confirms about our gaps

| Our Gap | Ecosystem validation |
|---|---|
| **HX-20 (claim semantics)** | Paperclip built their entire product around atomic task checkout as the #1 pain point. Multica has `EmptyClaimCache` (Redis fast-path) for queue contention. Gastown has no atomic claim — it's their reported pain too. |
| **HX-15 (DELEGATE step)** | Boomerang Tasks is the same pattern in production at Roo Code. Validated. |
| **GAP-07 (PolicyEngine)** | Omnigent's three-tier policies (server/agent/session) — confirmed critical for team use. Paperclip's org chart budgets confirm governance is a top user demand. |
| **GAP-03 (real HITL Slack)** | Universally missing or stub in every peer project. HorizonX implementing it is a genuine differentiator. |
| **GAP-13/14 (knowledge base + curator)** | Hermes (207k stars) built their entire identity around this. Ruflo's SONA adds trajectory-based learning on top. We're implementing the right thing. |
| **GAP-04 (cross-session spin)** | ECC's 5-layer observer loop prevention and Ruflo's trajectory-based SONA both confirm spin detection is a real production need. Our 6-layer detector + cross-session layer is best-in-class. |

### What HorizonX does better than every peer project

| Capability | Status in peers | HorizonX |
|---|---|---|
| Crash-safe dual-write (SQLite + JSONL) | None have it | ✓ Implemented |
| Dangling tool-call repair before API resume | None have it | ✓ Implemented (`agents/repair.py`) |
| Session resume via native agent IDs | Most restart from scratch | ✓ Claude Code + Codex |
| Validator-gated status transitions | None have it | ✓ Goal graph privilege split |
| 6-layer spin detection | ECC has 5-layer (loop prevention only); most have 0 | ✓ Implemented + improving (HX-19) |
| Strategy composability (8 strategies) | CrewAI Flows, LangGraph stateful; others are single-mode | ✓ Implemented |

### One new gap surfaced: A2A Protocol interop

The **Agent-to-Agent (A2A) protocol** launched December 2025 and is being adopted by ECC, Microsoft Agent Framework, and Multica. It defines a standard wire format for agent-to-agent delegation, capability advertisement, and result handoff. Without it, HorizonX runs in isolation — it can't receive tasks from or delegate to other orchestrators in a multi-org deployment.

This becomes **GAP-20** — tracked in the roadmap as P3 after the core is solid.

---

### GAP-20 · No A2A protocol support — HorizonX is an island

**What it is:** The [A2A protocol](https://google.github.io/A2A/) is a JSON-based wire standard for agent interoperability: capability advertisement (what can this agent do), task delegation (send a task to another agent), streaming result handoff (SSE), and push notifications. It sits above MCP (which handles tool calls) and provides agent-to-agent coordination semantics.

**Who's adopting it:** Google, Microsoft (Agent Framework GA mid-2026), ECC, Multica, Anthropic Agent SDK. Teams in production are being pushed toward A2A for cross-org agent handoffs.

**User pain:** A company using HorizonX for long-horizon coding tasks wants to delegate a sub-task to a research agent running in a different team's orchestrator. Without A2A, this requires custom HTTP plumbing. With A2A, it's a standard `tasks/send` call.

**Value of fix (scoped, not full A2A stack):** 
- Add an `A2AAgentDriver` that wraps any A2A-compliant remote agent as a HorizonX agent driver — task goes out over A2A, steps come back as SSE, result lands in the goal graph like any local agent
- Add an `A2AServer` endpoint to the dashboard: expose HorizonX runs as A2A-compliant tasks so other orchestrators can delegate INTO HorizonX
- This positions HorizonX as the durable execution layer in a multi-orchestrator architecture

**Effort:** 4 days. **Priority: P3.**

---

*This document is the basis for a phased implementation plan. Phase 0 bugs should be the first PR; Phase 1 defines the v1.0 scope. Updated 2026-07-01 with ecosystem research across 20+ active multi-agent projects.*
