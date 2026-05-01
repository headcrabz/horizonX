# HorizonX — A Long-Horizon Agent Execution Harness

> The complete design document. Concepts → Architecture → Strategies → Context → Anti-Cycling → Operations → Use Cases → Roadmap. One file.

---

## Table of contents

**Part I — Foundations**
1. [What is a long-horizon agent execution harness?](#1-what-is-a-long-horizon-agent-execution-harness)
2. [Mental model — three layers](#2-mental-model--three-layers)
3. [What a harness IS](#3-what-a-harness-is)
4. [What a harness is NOT](#4-what-a-harness-is-not)
5. [The 14 failure points without a harness](#5-the-14-failure-points-without-a-harness)
6. [The 8 design principles](#6-the-8-design-principles)
7. [The 4 cross-cutting capabilities (audit · fork · retry · HITL)](#7-the-4-cross-cutting-capabilities)
8. [When you do NOT need a harness](#8-when-you-do-not-need-a-harness)

**Part II — HorizonX architecture**
9. [Layered architecture](#9-layered-architecture)
10. [Core types](#10-core-types)
11. [Runtime — the central orchestrator](#11-runtime--the-central-orchestrator)
12. [Goal graph — durable hierarchical plan](#12-goal-graph--durable-hierarchical-plan)
13. [Session manager — bounded agent invocations](#13-session-manager--bounded-agent-invocations)
14. [Filesystem handoffs](#14-filesystem-handoffs)
15. [Summarizer — context compression](#15-summarizer--context-compression)
16. [Trajectory recorder](#16-trajectory-recorder)
17. [Database schema](#17-database-schema)
18. [Event bus](#18-event-bus)
19. [Resource governor](#19-resource-governor)
20. [Run state machine](#20-run-state-machine)

**Part III — Strategies**
21. [The 8 execution strategies](#21-the-8-execution-strategies)
22. [Composing strategies](#22-composing-strategies)
23. [Writing your own strategy](#23-writing-your-own-strategy)

**Part IV — Pluggable layers**
24. [Agent drivers (Claude Code, Codex, OpenHands, Custom)](#24-agent-drivers)
25. [Milestone validators — gates not graders](#25-milestone-validators--gates-not-graders)
26. [Spin detection — multi-layer anti-cycling](#26-spin-detection--multi-layer-anti-cycling)
27. [Retry strategies](#27-retry-strategies)
28. [Early stopping](#28-early-stopping)

**Part V — Operations**
29. [Auditability](#29-auditability)
30. [Forkable runs](#30-forkable-runs)
31. [Retryability](#31-retryability)
32. [HITL gates](#32-hitl-gates)
33. [Observability and live monitoring](#33-observability-and-live-monitoring)

**Part VI — Use cases**
34. [Use case 1 — Coding (build OAuth)](#34-use-case-1--coding-build-oauth)
35. [Use case 2 — ML training (Karpathy autoresearch wrapped)](#35-use-case-2--ml-training)
36. [Use case 3 — SRE monitoring](#36-use-case-3--sre-monitoring)
37. [Use case 4 — Complex decision (M&A due diligence)](#37-use-case-4--complex-decision)
38. [Use case 5 — Migration (monolith → microservices)](#38-use-case-5--migration)
39. [Use case 6 — Content (technical book)](#39-use-case-6--content)
40. [Use case 7 — Research synthesis](#40-use-case-7--research-synthesis)

**Part VII — Implementation**
41. [Skeleton code](#41-skeleton-code)
42. [Implementation roadmap](#42-implementation-roadmap)
43. [References](#43-references)

---

# Part I — Foundations

## 1. What is a long-horizon agent execution harness?

A **long-horizon agent execution harness** is the production runtime that wraps an LLM-based agent (Claude Code, Codex CLI, OpenHands, etc.) so it can reliably execute tasks that take **hours to days, span dozens of context windows, involve hundreds of steps, and require recovery from inevitable failures**.

It is to agents what Temporal and Airflow are to deterministic workflows — durable state, retries, observability, bounded execution — but designed for the **probabilistic, drift-prone, context-limited** nature of LLM agents.

The harness does not reason. The agent reasons. The harness *makes the reasoning matter for hours instead of minutes*.

> *"Out of the box, even a frontier coding model like Opus 4.5 running on the Claude Agent SDK in a loop across multiple context windows will fall short."*
> — Anthropic, [Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)

The model is not the bottleneck. The harness is.

---

## 2. Mental model — three layers

```
┌────────────────────────────────────────────┐
│             LLM (the brain)                │   ← reasoning, language
│       Opus / Sonnet / GPT-5 / etc.         │
└────────────────────────────────────────────┘
                    ↓
┌────────────────────────────────────────────┐
│         Agent CLI/SDK (the hands)          │   ← tool use, file edits, shell
│      Claude Code · Codex · OpenHands       │
└────────────────────────────────────────────┘
                    ↓
┌────────────────────────────────────────────┐
│   Harness (the executive function)         │   ← planning, memory, oversight
│              HorizonX                      │
└────────────────────────────────────────────┘
```

| Component | Owns | Time scale |
|---|---|---|
| LLM | Reasoning over a single prompt | Seconds |
| Agent | A single bounded session of tool use | Minutes |
| Harness | The entire long-running task | Hours to days |

When you only have the first two, you get great single-session agents that lose the plot at hour one. Adding a harness is what turns *capability* into *reliability*.

---

## 3. What a harness IS

A harness, properly built, provides nine capabilities:

1. **Persistent goal model.** A durable graph of goals/sub-goals that survives context windows, crashes, and restarts. The agent reads it at every session start.
2. **Bounded sessions.** Hard limits per agent invocation (steps, minutes, tokens, context utilization). When a limit is hit, the session closes cleanly — it doesn't crash.
3. **Filesystem-mediated handoffs.** State passes between sessions through files (`progress.md`, `goals.json`, `decisions.jsonl`, `summary.md`), not through ever-growing context.
4. **Milestone validators.** Gates that run between sessions — tests pass? build works? metric improved? — and decide *continue / pause / retry / abort*.
5. **Spin / cycle detection.** Multi-layer detection of repetition, oscillation, edit-revert loops, score plateaus, and semantic stagnation.
6. **Resource governance.** Hard budgets on wall-clock, tokens, USD. Threshold notifications at 50/75/90%. Automatic abort at 100%.
7. **Crash recovery.** Every event persists immediately. Restart resumes from the last session boundary, often via session-resume APIs.
8. **HITL escalation.** Defined triggers (validator fail, spin, budget threshold, ambiguity) pause the run and route to a human with full context.
9. **Observability.** Real-time event stream of every step, every decision, every state change — to terminal, web dashboard, Slack, or custom hook.

These nine are non-negotiable. A "harness" missing any of them isn't a harness — it's a bash script that calls Claude in a loop.

---

## 4. What a harness is NOT

Confusion with adjacent things is the most common reason teams build the wrong thing. Sharp boundaries:

| **NOT** | Difference |
|---|---|
| **An eval harness** (SWE-bench, AgentBench, Inspect AI) | Eval harnesses *measure* agents on benchmarks. Execution harnesses *run* agents on real work. |
| **An agent framework** (LangGraph, AutoGen, CrewAI) | Frameworks define how an agent thinks. Harnesses define how it's *managed*. You can use a framework-built agent inside a harness. |
| **An LLM wrapper / chat client** | Cline, Cursor, Claude.ai are interactive, no durability. A harness runs autonomously for hours. |
| **A workflow engine** (Temporal, Airflow, Prefect) | Workflow engines run *deterministic* tasks. Harnesses run *probabilistic* tasks where the agent decides the steps. |
| **A sandbox** (Docker, Podman, E2B) | A harness *uses* a sandbox; it is not one. |
| **A prompt manager** (LangSmith, Helicone) | Prompt managers track prompts. A harness adds state, scheduling, validation, and recovery. |
| **An observability tool** (Langfuse, Phoenix) | Observability tools record what happened. A harness records *and* decides what happens next. |
| **An MCP server** | MCP servers expose tools. A harness orchestrates an agent that *uses* MCP servers. |

**Rule of thumb**: if the system runs a *single* agent invocation and stops, it's not a harness. If it runs *many bounded invocations against a persistent goal* and recovers from failure, it is.

---

## 5. The 14 failure points without a harness

Real long-horizon agent runs fail in predictable ways. A harness that doesn't address all of them is incomplete.

| # | Failure | Symptom | Root cause |
|---|---|---|---|
| 1 | Context exhaustion | Agent forgets earlier work, repeats steps | Single context window for a multi-hour task |
| 2 | Premature completion | Agent says "done" with most work undone | No structural gate on completion claim |
| 3 | Cyclic loops | Same tool call 50 times, no progress | No loop detection |
| 4 | Edit-revert oscillation | File changed and changed back repeatedly | No diff-based oscillation guard |
| 5 | Plan drift | Final output differs from initial plan | No durable plan to anchor to |
| 6 | Test deletion | Agent removes failing tests instead of fixing code | No structural prohibition |
| 7 | Silent stagnation | Lots of activity, no real progress | No metric / progress gate |
| 8 | Crash = total loss | 4-hour task fails at hour 3, restart from zero | No durable state, no resume |
| 9 | Cost runaway | $1000 token bill with nothing to show | No resource budget |
| 10 | Operator blindness | Can't tell if an 8-hour run is healthy at hour 4 | No real-time observability |
| 11 | Brittle handoffs | New session has no idea what the previous one did | No structured handoff layer |
| 12 | Validation theater | Tests "pass" because agent rewrote them to be trivial | No anti-gaming guard |
| 13 | Tool overuse | Uses `bash` for everything when better tools exist | No allowlist or budget per tool |
| 14 | Permission creep | Agent does dangerous things without checks | No HITL gate on destructive ops |

A harness exists to make failures 1–14 **structural impossibilities**, not aspirational best practices.

---

## 6. The 8 design principles

If you're building a long-horizon agent harness — HorizonX or otherwise — these have to be true.

### 6.1 State is durable, not contextual
Working memory belongs on disk and in a database, not in the agent's context window. If the run survives a power loss, the design is right.

### 6.2 Sessions are bounded by hard limits
No session ever runs unbounded. Every agent invocation has a step cap, a wall-clock cap, a token cap, and a context-utilization cap. When *any* limit is approached, the session closes cleanly. **Bounded sessions chained through state** is the only thing that works at long horizons.

### 6.3 The filesystem is the handoff layer
Not the context, not the model, not RAM. Files. `progress.md`, `goals.json`, `decisions.jsonl`, `failures.jsonl`. The agent reads them at session start, writes them at session end. The harness verifies the discipline.

### 6.4 Validators gate, they don't grade
A validator returns `Continue / Pause / Abort / RetryWithModification`. Score is informational; the *decision* is the contract.

### 6.5 Status transitions are owned by the runtime, not the agent
The agent can *propose* a goal is done. The runtime decides — only after validators confirm.

### 6.6 Tests and structural artifacts are sacred
Strongly-worded prompts ("It is unacceptable to remove or edit tests") + structural enforcement (git pre-commit hook fails the session if `tests/` shrinks).

### 6.7 Escalate early, escalate cheaply
A 5-minute HITL pause beats 3 hours of an agent burning tokens on the wrong path. Define triggers explicitly.

### 6.8 Observability is the user interface
A long-horizon run is autonomous, but the *operator* must never be in the dark. SSE/WebSocket event stream + terminal `watch` UI + web dashboard + Slack.

---

## 7. The 4 cross-cutting capabilities

Beyond the principles, four capabilities are *operational requirements* — they're how teams actually use a harness in production.

### 7.1 Auditable
Every step, every decision, every state change persists with timestamp, actor, and rationale. Months later, you can answer: *what did this agent do? why? on what evidence?* Append-only `decisions.jsonl`, immutable `steps` table, durable git history. **See §29.**

### 7.2 Forkable
Fork a run at any session boundary. Try alternative strategies from a known state. Compare branches. Merge winners. Enables `TreeOfTrials` and safe A/B testing. **See §30.**

### 7.3 Retryable
Multiple retry strategies, picked by failure mode: naive · mutation · decomposition · escalation. **See §27 and §31.**

### 7.4 HITL-ready
First-class human-in-the-loop. Defined triggers, full-context handoff to operator, structured response (approve / modify / abort / re-decompose), seamless resume. **See §32.**

These four (`audit · fork · retry · HITL`) move a harness from "demo" to "production tool."

---

## 8. When you do NOT need a harness

Use the right tool. *Don't* use a long-horizon harness for:

- **Single-shot questions** (`"explain this function"`) — use the agent CLI directly
- **Sub-30-step tasks** (`"fix this bug"`) — Claude Code or Cursor are enough
- **Interactive sessions** with a human in every step — chat UI is correct
- **Deterministic pipelines** with no LLM in the loop — Airflow / Prefect / Temporal
- **High-frequency low-latency tasks** — overhead of session management dominates

Break-even: **>30 steps, >15 minutes, expect-to-resume-after-failure**. Below that, simpler tools win.

---

# Part II — HorizonX architecture

## 9. Layered architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  Layer 6 — Interface     CLI · SDK · Web Dashboard · Slack           │
├──────────────────────────────────────────────────────────────────────┤
│  Layer 5 — Strategy      Sequential · Ralph · Tree · Monitor · Pair   │
│                          Decomposition · SelfCritique · SingleSession │
├──────────────────────────────────────────────────────────────────────┤
│  Layer 4 — Orchestrator  Runtime · SessionManager · GoalGraph        │
├──────────────────────────────────────────────────────────────────────┤
│  Layer 3 — Plug-ins      Agents · Validators · Summarizer · HITL     │
├──────────────────────────────────────────────────────────────────────┤
│  Layer 2 — Observability TrajectoryRecorder · SpinDetector · Bus     │
├──────────────────────────────────────────────────────────────────────┤
│  Layer 1 — Persistence   Store (SQLite / Postgres) · WorkspaceFS     │
└──────────────────────────────────────────────────────────────────────┘
```

**Strict layering rule**: Layer N depends only on Layers ≤ N. The Runtime in L4 calls into L3 plugins through small protocols and never imports any specific plugin. Strategies in L5 compose L4 primitives and never reach below L4 directly.

---

## 10. Core types

All core types live in `horizonx/core/types.py` and are immutable Pydantic models. They flow as JSON between processes and persist to the durable store unchanged.

```python
RunId      = NewType("RunId", str)        # uuid4
SessionId  = NewType("SessionId", str)    # uuid4
GoalId     = NewType("GoalId", str)       # slug like "g.oauth.pkce"
StepId     = NewType("StepId", str)       # uuid4

class Task(BaseModel):
    id: str
    name: str
    prompt: str
    horizon_class: Literal["short","medium","long","very_long"]
    estimated_duration_hours: tuple[float, float] | None
    strategy: StrategyConfig
    agent: AgentConfig
    environment: EnvironmentConfig
    milestone_validators: list[ValidatorConfig]
    handoff_files: list[str]
    summarizer: SummarizerConfig | None
    spin_detection: SpinDetectionConfig
    hitl: HITLConfig | None
    resources: ResourceLimits

class Run(BaseModel):
    id: RunId
    task: Task
    parent_run_id: RunId | None             # for forks
    status: Literal["PENDING","RUNNING","PAUSED_HITL","COMPLETED","FAILED","ABORTED","TIMED_OUT"]
    started_at: datetime
    completed_at: datetime | None
    workspace_path: Path
    current_session_id: SessionId | None
    goal_graph_root: GoalId
    cumulative: CumulativeMetrics            # tokens, cost, wall-clock

class Session(BaseModel):
    id: SessionId
    run_id: RunId
    sequence_index: int
    target_goal_id: GoalId | None
    started_at: datetime
    completed_at: datetime | None
    status: Literal["RUNNING","COMPLETED","TIMEOUT","OOC","ERRORED","SPIN"]
    steps_count: int
    tokens_used: int
    agent_session_id: str | None             # Claude Code / Codex session for resume
    handoff_summary_path: Path

class Step(BaseModel):
    id: StepId
    session_id: SessionId
    sequence: int
    type: Literal["THOUGHT","TOOL_CALL","OBSERVATION","ERROR","MILESTONE","HITL_PAUSE","SPIN"]
    tool_name: str | None
    content: dict
    timestamp: datetime
    duration_ms: int | None

class GoalNode(BaseModel):
    id: GoalId
    parent_id: GoalId | None
    name: str
    description: str
    verification_criteria: list[str]
    status: Literal["PENDING","IN_PROGRESS","DONE","FAILED","BLOCKED","SKIPPED"]
    children: list[GoalId]
    attempts: int
    notes: str
    last_updated_at: datetime
    last_updated_by_session: SessionId | None
```

---

## 11. Runtime — the central orchestrator

`horizonx/core/runtime.py` exposes a single class. The Runtime is **strategy-agnostic** — it provides primitives. Strategies decide when to call them.

```python
class Runtime:
    """Top-level orchestrator. One Runtime serves N concurrent Runs."""

    def __init__(self, db_url: str, event_bus: EventBus | None = None):
        self.store = Store.from_url(db_url)
        self.bus = event_bus or InMemoryBus()

    async def run(self, task: Task, *, resume_from: RunId | None = None) -> Run:
        run = await self._load_or_create(task, resume_from)
        strategy = StrategyRegistry.get(task.strategy.kind)
        async with self._governor(run):
            async for event in strategy.execute(run, self):
                await self._handle(event)
        return run

    # Strategies and validators call these primitives:
    async def start_session(self, run, target_goal) -> Session: ...
    async def end_session(self, session, status) -> None: ...
    async def record_step(self, session, step) -> None: ...
    async def run_validators(self, run, when) -> list[ValidationResult]: ...
    async def check_spin(self, session) -> SpinReport: ...
    async def emit(self, event) -> None: ...
    async def request_hitl(self, run, reason, context) -> HITLDecision: ...
    async def fork_run(self, parent, mutation) -> Run: ...
    async def merge_run(self, parent, child) -> None: ...
```

---

## 12. Goal graph — durable hierarchical plan

The goal graph is HorizonX's answer to **"how do you keep an agent on track for hours?"** It comes from Anthropic's pattern in their long-running-agents article.

### Philosophy
- Single source of truth for **what is happening**.
- Stored as `goals.json` in the run workspace and mirrored to the `goals` table.
- Agent reads at every session start; writes only `notes` and proposes `status`.
- Strongly-worded prompts ("only modify the `notes` field of YOUR sub-goal") + structural enforcement (`GoalGraphGate`).

### Format

```json
{
  "version": 1,
  "root": "g.root",
  "nodes": {
    "g.root": {
      "name": "Implement OAuth 2.0 system",
      "description": "Authorization Code flow with PKCE, refresh, revocation",
      "children": ["g.auth", "g.token", "g.tests", "g.docs"],
      "status": "in_progress",
      "verification_criteria": [
        "All 4 sub-goals marked done",
        "Integration test passes end-to-end",
        "Build succeeds",
        "Security scan finds no critical issues"
      ],
      "attempts": 0,
      "notes": ""
    },
    "g.auth": {
      "parent_id": "g.root",
      "name": "Authorization endpoint with PKCE",
      "description": "Implement /authorize accepting code_challenge",
      "verification_criteria": [
        "POST /authorize accepts code_challenge",
        "Returns redirect with code parameter",
        "test_authorize_pkce passes"
      ],
      "children": ["g.auth.handler", "g.auth.pkce", "g.auth.test"],
      "status": "in_progress",
      "attempts": 1,
      "notes": "Started in session 3. Picked PKCE-S256 over plain. Stuck on cookie domain config."
    }
  }
}
```

### Invariants (enforced by `GoalGraphGate`)
1. One root, named `g.root`.
2. DAG only — no cycles.
3. Status transitions are monotonic (`pending → in_progress → done` or `→ failed`).
4. Only the agent writes `notes` and proposes `status`. Runtime owns actual transitions.
5. All leaf goals are atomic (fit in one session).

### Operations

| Op | Performed by | When |
|---|---|---|
| `decompose(parent)` | Initializer or re-decomposer agent | Setup; failed goals after retry |
| `next_leaf()` | Runtime + Strategy | Each session start |
| `mark_in_progress(id)` | Runtime (auto) | Session start |
| `update_notes(id, notes)` | Agent (mid-session) | Free text, append-only feel |
| `propose_done(id)` | Agent (end of session) | Records intent only |
| `mark_done(id)` | Runtime | After validators pass |
| `mark_failed(id)` | Runtime | Validator fail or session error past max_attempts |
| `re_decompose(id)` | Re-decomposer agent or HITL | When max_attempts exceeded |

### Why agents can't mark `done` themselves
The single biggest failure mode is **premature completion**. The agent says *"I think I'm done"* via `propose_done`. The Runtime accepts only after milestone validators all pass. This mirrors Anthropic's discipline: *"It is unacceptable to remove or edit tests."*

### Hierarchy depth
2–4 levels:
- **Level 0** (root) — the whole task
- **Level 1** — major sub-systems
- **Level 2** — atomic features
- **Level 3** — sub-tasks if needed

Anthropic's example used a flat list of 200 features. HorizonX supports both flat and deep.

---

## 13. Session manager — bounded agent invocations

A long task is **N bounded sessions chained through filesystem state**, not one giant context window.

```python
class SessionManager:
    async def open(self, run: Run, target_goal: GoalNode) -> Session:
        # Compose system + user prompt from:
        # 1. base prompt template
        # 2. goal graph (loaded from goals.json)
        # 3. progress.md (last 100 lines)
        # 4. decisions.jsonl (last 20 entries)
        # 5. failures.jsonl (matching this goal)
        # 6. summary.md (from previous session)
        # 7. session checklist (Anthropic pattern)
        prompt = self._compose_session_prompt(run, target_goal)
        agent_session_id = await self._maybe_resume(run, session)
        return session

    async def close(self, session: Session, outcome: SessionOutcome) -> None:
        # Mandatory cleanup (Anthropic pattern):
        # 1. agent writes summary.md
        # 2. agent commits to git with descriptive message
        # 3. agent updates progress.md with what was done
        # 4. agent updates goals.json (notes only)
        # 5. milestone validators run
        # 6. Runtime evaluates outcome → mark goal done/failed
        await self._enforce_cleanup_protocol(session)
        await self._persist(session, outcome)
```

### Per-session limits

Every session is bounded by **the smaller of**:
- `max_steps` (default 50)
- `max_minutes` wall clock (default 25)
- `max_tokens` budget
- Context window utilization < 70%

When a limit is hit, the session **does not abort** — it triggers an **orderly close**: agent is instructed to write summary, commit, update progress. A new session opens to continue.

### Session prompt skeleton

```
You are working on: <task.name>

CURRENT SUB-GOAL:
  ID: g.auth.handler
  Description: Implement the /authorize endpoint handler...
  Verification: <criteria>

REQUIRED SESSION STARTUP CHECKLIST:
  1. Run: pwd
  2. Read: progress.md
  3. Read: goals.json
  4. Read: failures.jsonl (filter by your goal_id)
  5. Run: git log --oneline -20
  6. Run: ./init.sh
  7. Test current functionality is NOT broken before changes

REQUIRED SESSION CLEANUP:
  1. Write: summary.md
  2. Run: git add -A && git commit -m "<descriptive>"
  3. Append: progress.md
  4. Append: decisions.jsonl
  5. Append: failures.jsonl
  6. Update goals.json `notes` field for your sub-goal
  7. Use propose_done(goal_id) ONLY if you believe verification passes

DISCIPLINE:
  - You may modify only `notes` and propose `status` for YOUR sub-goal.
  - You may NOT delete or edit tests.
  - You may NOT mark goals `done` directly — that's the Runtime's job.

CONTEXT FROM PREVIOUS SESSIONS:
  <last summary.md>
  <last 100 lines of progress.md>
  <last 20 entries of decisions.jsonl>
  <failures.jsonl entries for this goal>

LIMITS:
  Max steps: 50
  Max minutes: 25

YOUR INITIAL INSTRUCTIONS:
  <user prompt>
```

This template is **not** prompt engineering — it's a runtime contract. Every field is enforced by the Runtime.

---

## 14. Filesystem handoffs

Every long-horizon run keeps four files in the run workspace.

### `progress.md` — narrative log (append-only)

```markdown
## Session 1 — 2026-04-30 14:23 UTC (Initializer)
- Decomposed root goal into 4 sub-goals: auth, token, tests, docs
- Created goals.json with 12 leaf nodes
- Wrote init.sh that starts uvicorn on :8000

## Session 2 — 2026-04-30 14:56 UTC (Sub-goal: g.auth.handler)
- Read goals.json, picked g.auth.handler
- Implemented /authorize endpoint in app/oauth/authorize.py
- Added PKCE-S256 support (chose over plain per OAuth 2.1 draft)
- pytest tests/test_authorize.py: 3/3 pass
- git commit "feat(oauth): /authorize endpoint with PKCE"
```

### `decisions.jsonl` — append-only decision record

```jsonl
{"ts":"2026-04-30T14:34:18Z","sess":"s.2","goal":"g.auth.handler","decision":"chose PKCE-S256","rationale":"OAuth 2.1 draft deprecates plain"}
{"ts":"2026-04-30T14:42:01Z","sess":"s.2","goal":"g.auth.handler","decision":"used FastAPI Depends() for token store","rationale":"matches existing pattern"}
```

This is **the audit trail** for the agent's reasoning.

### `failures.jsonl` — what we tried that didn't work

```jsonl
{"ts":"2026-04-30T15:12:45Z","goal":"g.auth.pkce","attempt":"setting cookie domain=None","outcome":"broke existing session login"}
```

Future sessions read this file and avoid retrying the same failed approaches.

### `summary.md` — last session's compressed handoff
The Summarizer (§15) produces this at session close.

---

## 15. Summarizer — context compression

When a session approaches its token budget or context window, the Summarizer compresses recent trajectory into a 1-page handoff.

### Triggers
- Context utilization > 70% — proactive
- Session about to close — always summarize before close
- Manual via `runtime.summarize(session)`

### Output (structured, not just prose)

```python
class SessionSummary(BaseModel):
    session_id: SessionId
    target_goal_id: GoalId | None
    summary_md: str                      # 1-page narrative
    key_decisions: list[str]
    blockers: list[str]
    next_actions: list[str]
    files_modified: list[str]
    tests_status: dict
    confidence: float                    # 0..1
```

The summary is written to `summary.md` AND structured fields stored in DB for queryability. **Structured > prose** — the next session prompt has clean injection points (decisions, blockers, next actions); the Runtime can highlight blockers with `⚠️`, cross-check against the goal graph, and warn on test regressions.

### Why this matters
This is what Anthropic means by *"compaction isn't sufficient."* HorizonX's summarizer extracts structure, not just text. It uses a cheap model (Haiku 4.5 by default) — context compression doesn't need a frontier model.

---

## 16. Trajectory recorder

Every step persists immediately, not at session end.

```python
class TrajectoryRecorder:
    async def record(self, session: Session, step: Step) -> None:
        await self._append_jsonl(session, step)    # JSONL on disk (truth)
        await self.store.insert_step(step)          # DB (queryable)
        await self.bus.publish(StepEvent(step))     # live observers
```

JSONL is the **source of truth on disk** — append-only, survives DB corruption. The DB is the **query interface**. Both must agree; reconciliation runs at startup.

---

## 17. Database schema

SQLite default; same DDL works for PostgreSQL with minor tweaks (`JSON` → `JSONB`, `TEXT` PKs → `UUID`).

```sql
CREATE TABLE runs (
    id              TEXT PRIMARY KEY,
    parent_run_id   TEXT REFERENCES runs(id),       -- forks
    task_id         TEXT NOT NULL,
    task_snapshot   JSON NOT NULL,                   -- full Task at start (reproducibility)
    status          TEXT NOT NULL,
    workspace_path  TEXT NOT NULL,
    started_at      TIMESTAMP NOT NULL,
    completed_at    TIMESTAMP,
    current_session_id TEXT,
    goal_graph_root TEXT NOT NULL,
    cumulative      JSON NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_runs_status ON runs(status, started_at);

CREATE TABLE sessions (
    id                TEXT PRIMARY KEY,
    run_id            TEXT NOT NULL REFERENCES runs(id),
    sequence_index    INTEGER NOT NULL,
    target_goal_id    TEXT,
    status            TEXT NOT NULL,
    started_at        TIMESTAMP NOT NULL,
    completed_at      TIMESTAMP,
    steps_count       INTEGER NOT NULL DEFAULT 0,
    tokens_used       INTEGER NOT NULL DEFAULT 0,
    agent_session_id  TEXT,
    handoff_summary_path TEXT
);
CREATE INDEX idx_sessions_run ON sessions(run_id, sequence_index);

CREATE TABLE steps (
    id           TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL REFERENCES sessions(id),
    sequence     INTEGER NOT NULL,
    type         TEXT NOT NULL,
    tool_name    TEXT,
    content      JSON NOT NULL,
    timestamp    TIMESTAMP NOT NULL,
    duration_ms  INTEGER
);
CREATE INDEX idx_steps_session ON steps(session_id, sequence);

CREATE TABLE goals (
    id                       TEXT PRIMARY KEY,
    run_id                   TEXT NOT NULL REFERENCES runs(id),
    parent_id                TEXT REFERENCES goals(id),
    name                     TEXT NOT NULL,
    description              TEXT NOT NULL,
    verification_criteria    JSON NOT NULL,
    status                   TEXT NOT NULL,
    attempts                 INTEGER NOT NULL DEFAULT 0,
    notes                    TEXT,
    last_updated_at          TIMESTAMP NOT NULL,
    last_updated_by_session  TEXT REFERENCES sessions(id)
);
CREATE INDEX idx_goals_run ON goals(run_id, status);

CREATE TABLE validations (
    id           TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL REFERENCES runs(id),
    session_id   TEXT REFERENCES sessions(id),
    validator    TEXT NOT NULL,
    decision     TEXT NOT NULL,                      -- continue|pause|abort|retry_with_mod
    reason       TEXT NOT NULL,
    score        REAL,
    details      JSON,
    started_at   TIMESTAMP NOT NULL,
    duration_ms  INTEGER
);

CREATE TABLE hitl_events (
    id           TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL REFERENCES runs(id),
    triggered_at TIMESTAMP NOT NULL,
    trigger      TEXT NOT NULL,
    context      JSON NOT NULL,
    resolved_at  TIMESTAMP,
    decision     TEXT,
    operator     TEXT,
    instruction  TEXT
);

CREATE TABLE spin_reports (
    id           TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL REFERENCES sessions(id),
    layer        TEXT NOT NULL,
    detected_at  TIMESTAMP NOT NULL,
    detail       JSON NOT NULL,
    action_taken TEXT NOT NULL
);
```

---

## 18. Event bus

Every state-change emits an event.

```python
class Event(BaseModel):
    type: EventType
    run_id: RunId
    session_id: SessionId | None
    timestamp: datetime
    payload: dict

EventType = (
    "run.started" | "run.completed" | "run.failed" | "run.paused_hitl" |
    "session.started" | "session.completed" | "session.timeout" |
    "step.recorded" |
    "goal.in_progress" | "goal.done" | "goal.failed" |
    "validator.passed" | "validator.failed" |
    "spin.detected" |
    "hitl.requested" | "hitl.resolved" |
    "budget.threshold" |
    "summary.created" |
    "fork.created" | "fork.merged"
)
```

Default in-memory pub/sub for single-process; swap in Redis/NATS for multi-process.

---

## 19. Resource governor

Enforces hard budgets.

```python
class ResourceGovernor:
    def __init__(self, limits: ResourceLimits):
        self.limits = limits
        self.consumed = ResourceConsumed()

    def charge(self, *, tokens=0, usd=0.0, seconds=0.0):
        self.consumed += (tokens, usd, seconds)
        if self.consumed.exceeds_threshold(self.limits, 0.5):
            self.bus.publish(BudgetEvent(50))
        if self.consumed.exceeds(self.limits):
            raise BudgetExceeded(self.consumed)
```

Budgets: wall-clock seconds · total tokens · total USD (model-priced) · per-session and per-run. Notifications at 50%, 75%, 90%, 100%.

---

## 20. Run state machine

```
            ┌──────────┐
            │ PENDING  │
            └─────┬────┘
                  │ run()
                  ↓
            ┌──────────┐
       ┌────│ RUNNING  │────┐
       │    └─────┬────┘    │
       │          │         │
   spin/validator │         │ done
       │          │         │
       ↓          ↓         ↓
  ┌────────┐ ┌──────────┐ ┌──────────┐
  │PAUSED_ │ │  FAILED  │ │COMPLETED │
  │ HITL   │ │          │ │          │
  └───┬────┘ └──────────┘ └──────────┘
      │
      │ resolve
      ↓
  ┌─────────┐
  │RUNNING  │  (or → ABORTED)
  └─────────┘
```

---

# Part III — Strategies

## 21. The 8 execution strategies

A *Strategy* decides **which sub-goal to attempt next, when to validate, when to retry, and when to give up**. Same primitives, different patterns.

### Decision matrix

| Task shape | Pick |
|---|---|
| Small one-shot task (<30 steps) | **SingleSession** |
| Build a feature, migrate code, write a report | **SequentialSubgoals** |
| Optimize a metric, search a space | **RalphLoop** |
| Hard problem, want diverse approaches | **TreeOfTrials** |
| React to events / metrics over time | **MonitorRespond** |
| Very complex, high-stakes, ambiguous | **DecompositionFirst** → Sequential |
| Quality-critical (security, regulated) | **PairProgramming** |
| Code quality uplift, iterative refinement | **SelfCritique** |

### 21.1 SingleSession
One agent invocation runs to completion. No goal graph, no checkpoints. Use for simple tasks (<30 steps).

### 21.2 SequentialSubgoals (the Anthropic pattern)
Core long-horizon strategy. **One sub-goal per session. Filesystem handoffs. Mandatory checklists.**

```python
class SequentialSubgoals(Strategy):
    async def execute(self, run, rt):
        # Phase 1: initializer creates the goal graph
        if not run.has_goal_graph():
            init = await rt.start_session(run, target_goal=None)
            await rt.run_agent(init, INITIALIZER_PROMPT)
            await rt.parse_goal_graph_from_workspace(run)
            await rt.end_session(init, "completed")

        # Phase 2: iterate sub-goals
        while goal := rt.next_pending_leaf(run):
            session = await rt.start_session(run, target_goal=goal)
            result = await rt.run_agent(session, session_prompt(run, goal))
            decisions = await rt.run_validators(run, session, when="after_session")

            if any(d.decision == "pause_for_hitl" for d in decisions):
                hitl = await rt.request_hitl(run, decisions)
                if hitl.action == "abort": return
                if hitl.action == "modify": goal.notes += hitl.instruction

            if all(d.decision == "continue" for d in decisions):
                rt.mark_goal_done(goal)
            else:
                goal.attempts += 1
                if goal.attempts >= self.config.max_attempts_per_goal:
                    rt.mark_goal_failed(goal)

            await rt.end_session(session, result.status)
            yield SessionCompletedEvent(session.id)

        yield RunCompletedEvent(run.id, status="completed")
```

### 21.3 RalphLoop (the Karpathy autoresearch pattern)

Time-boxed iterative optimization. Each iteration: agent edits → run benchmark → measure → keep or discard.

Inspired by [Karpathy's autoresearch](https://github.com/karpathy/autoresearch): fixed 5-min training cycles, val_bpb metric drives keep/discard, ~12 experiments/hour, ~100 overnight.

```python
class RalphLoop(Strategy):
    async def execute(self, run, rt):
        baseline = await self._measure_baseline(run)
        best = baseline
        async for iter in self._iterate(self.config.total_minutes):
            session = await rt.start_session(run, target_goal=run.task.id)
            await rt.run_agent(session, RALPH_PROMPT.format(
                current_metric=best, budget_left=iter.budget_left))
            metric = await self._run_benchmark(run)
            if metric.improves_over(best):
                best = metric
                await rt.git_commit(run, f"iter {iter.index}: {metric}")
            else:
                await rt.git_reset(run, "HEAD")
            await rt.end_session(session, "completed")
            yield IterationCompletedEvent(iter.index, metric)
            if self._early_stop(best): break
```

**Mutable surface vs immutable infrastructure**: Like Karpathy's `train.py` vs `prepare.py` split. HorizonX enforces this via:
- `mutable_paths: ["train.py"]` in config
- Pre-iteration check: all other files unchanged
- Post-iteration `git diff` aborts if foreign files touched

### 21.4 TreeOfTrials

Spawn N parallel runs from same starting state, each with mutated config. Compare by metric. Best wins.

```python
class TreeOfTrials(Strategy):
    async def execute(self, run, rt):
        for round_idx in range(self.config.rounds):
            trials = self._generate_trials(run, round_idx)
            children = await asyncio.gather(*[
                rt.fork_run(run, mutation=t) for t in trials])
            reports = await asyncio.gather(*[
                rt.run_to_completion(c) for c in children])
            winner = self._pick_winner(reports)
            yield TrialRoundCompletedEvent(round_idx, winner)
            run = winner.run
        await rt.merge_winner_into(run)
```

**Mutation kinds**: prompt · model · strategy · decomposition · tool-allowlist.

### 21.5 MonitorRespond

Long-lived reactive agent. Subscribe to a stream of signals; react when triggers fire.

```python
class MonitorRespond(Strategy):
    async def execute(self, run, rt):
        async for event in self._signal_source(run.task):
            if not self._matches_trigger(event): continue
            goal = rt.create_reactive_goal(run, trigger=event)
            session = await rt.start_session(run, target_goal=goal)
            await rt.run_agent(session, REACTIVE_PROMPT.format(event=event))
            await rt.run_validators(run, session, when="after_session")
            await rt.end_session(session, "completed")
            yield ReactionCompletedEvent(event, goal.id)
```

Signal sources: PrometheusSource · WebhookSource · CronSource · FilesystemSource · SlackSource.

### 21.6 DecompositionFirst

Plan-only first session → HITL approval → execute via SequentialSubgoals.

```python
class DecompositionFirst(Strategy):
    async def execute(self, run, rt):
        plan = await rt.start_session(run, target_goal=None)
        await rt.run_agent(plan, DECOMP_PROMPT, allowed_tools=["Read","Write"])
        await rt.end_session(plan, "completed")

        hitl = await rt.request_hitl(run, reason="plan_review",
                                     context=rt.workspace_files(run, ["goals.json","plan.md"]))
        if hitl.action == "abort": return
        if hitl.action == "re_plan":
            yield from self.execute(run, rt); return
        if hitl.action == "modify":
            rt.apply_plan_modifications(run, hitl.instruction)

        seq = SequentialSubgoals(self.config.execution_config)
        async for event in seq.execute(run, rt):
            yield event
```

### 21.7 PairProgramming

Builder agent proposes; Critic agent gates. Highest-quality output for security/regulated work.

```python
class PairProgramming(Strategy):
    async def execute(self, run, rt):
        builder = AgentRegistry.get(self.config.builder)
        critic = AgentRegistry.get(self.config.critic)
        while goal := rt.next_pending_leaf(run):
            session = await rt.start_session(run, target_goal=goal)
            for proposal in builder.iter_proposals(session, goal):
                review = await critic.review(proposal)
                if review.accept:
                    await rt.execute_step(session, proposal)
                else:
                    await rt.record_rejection(session, proposal, review.reason)
                    builder.notify_rejection(review.reason)
                if rt.should_close_session(session): break
            await rt.end_session(session, "completed")
```

### 21.8 SelfCritique

Agent produces output; a critic (LLM, shell, or secondary agent) evaluates it; the agent iterates until the critic accepts or `max_rounds` is reached.

```python
class SelfCritique(Strategy):
    async def execute(self, run, rt):
        while goal := rt.next_pending_leaf(run):
            for round_idx in range(self.config.max_rounds):
                session = await rt.start_session(run, target_goal=goal)
                result = await rt.run_agent(session, session_prompt(run, goal))
                critique = await self._run_critic(run, session)

                if critique.score >= self.config.accept_threshold:
                    rt.mark_goal_done(goal)
                    await rt.end_session(session, "completed")
                    break

                # Inject critique into next session context
                goal.notes += f"\n\n## Critique (round {round_idx + 1})\n{critique.feedback}"
                await rt.end_session(session, "completed")
            else:
                rt.mark_goal_failed(goal)

            yield SessionCompletedEvent(session.id)
        yield RunCompletedEvent(run.id, status="completed")
```

**Critic types** (`critic_type` in config):
- `llm` — cheap model reviews output against a rubric (default)
- `shell` — shell command exit code + stdout feedback
- `agent` — secondary agent session acts as code reviewer

**Configuration** (`strategy.config`):

| Key | Default | Description |
|---|---|---|
| `max_rounds` | 5 | Maximum critique-iterate cycles per goal |
| `accept_threshold` | 0.85 | Score (0–1) at which the critic accepts |
| `critic_type` | `llm` | `llm` / `shell` / `agent` |
| `critic_model` | `claude-haiku-4-5` | Model when `critic_type=llm` |
| `critic_command` | null | Shell command when `critic_type=shell` |
| `write_progress` | `true` | Write `progress.md` after each round |

---

## 22. Composing strategies

Strategies are composable. A real run might look like:

```yaml
execution:
  strategy: composite
  pipeline:
    - DecompositionFirst:
        execution: { strategy: sequential_sub_goals }
    - TreeOfTrials:
        rounds: 2
        trials_per_round: 4
        inner_strategy: sequential_sub_goals
    - PairProgramming:
        builder: claude-code
        critic: codex
```

The `Composite` strategy runs each phase in sequence, passing run state forward.

---

## 23. Writing your own strategy

```python
class MyStrategy(Strategy):
    kind = "my_strategy"
    def __init__(self, config: MyConfig): self.config = config
    async def execute(self, run, rt) -> AsyncIterator[Event]:
        # Use rt primitives — get free observability, persistence,
        # spin detection, HITL, fork, retry, audit
        ...

# Register via entry point in pyproject.toml:
# [project.entry-points."horizonx.strategies"]
# my_strategy = "my_pkg.strategies:MyStrategy"
```

---

# Part IV — Pluggable layers

## 24. Agent drivers

Every agent implements the same protocol.

```python
class BaseAgent(Protocol):
    name: str
    async def run_session(
        self,
        session_prompt: str,
        workspace: Workspace,
        *,
        resume_session_id: str | None = None,
        on_step: Callable[[Step], Awaitable[None]] | None = None,
        cancel_token: CancelToken,
    ) -> SessionRunResult:
        ...
```

### 24.1 Claude Code driver — hyperoptimized

```python
class ClaudeCodeAgent(BaseAgent):
    """
    Wraps `claude -p ... --output-format stream-json --bare`.
    Leverages every Claude Code feature relevant to long-horizon execution.
    """
    name = "claude-code"

    async def run_session(self, session_prompt, workspace, *,
                          resume_session_id=None, on_step=None, cancel_token=None):
        cmd = ["claude", "-p", session_prompt,
               "--output-format", "stream-json", "--bare",
               "--model", self.config.model]

        # Constrain action space per task type
        if self.config.allowed_tools:
            cmd += ["--allowedTools", ",".join(self.config.allowed_tools)]

        # Extended thinking for hard reasoning steps
        if self.config.thinking_budget:
            cmd += ["--thinking", str(self.config.thinking_budget)]

        # Per-task MCP server injection
        if self.config.mcp_config_path:
            cmd += ["--mcp-config", str(self.config.mcp_config_path)]

        # Session resume across crashes
        if resume_session_id:
            cmd += ["--resume", resume_session_id]

        # Stream JSONL events → emit Steps in real-time
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=workspace.path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        await proc.stdin.drain()
        proc.stdin.close()

        captured_session_id = None
        async for line in proc.stdout:
            event = json.loads(line)
            step = self._parse_event(event)
            if event.get("type") == "session_id":
                captured_session_id = event["session_id"]
            if on_step: await on_step(step)
            if cancel_token.cancelled:
                proc.terminate(); break

        return SessionRunResult(
            agent_session_id=captured_session_id,
            status="completed" if proc.returncode == 0 else "errored")
```

**Features fully leveraged**:
- `--output-format stream-json` → real-time trajectory ingest
- `--thinking <budget>` for hard reasoning
- `--allowedTools` per task type (coding gets Edit+Bash; research gets WebSearch)
- `--bare` clean programmatic mode
- Session resume via saved session_id
- `--mcp-config` for per-task MCP servers
- Stdin prompt for long task descriptions

### 24.2 Codex driver — hyperoptimized


```python
class CodexAgent(BaseAgent):
    """
    Wraps `codex exec --json` and `codex exec resume <id>`.
    Pattern adapted from Atlas's CodexBridge.
    """
    name = "codex"

    async def run_session(self, session_prompt, workspace, *,
                          resume_session_id=None, on_step=None, cancel_token=None):
        if resume_session_id:
            cmd = ["codex", "exec", "resume", resume_session_id, "--json"]
        else:
            cmd = ["codex", "exec", "--json",
                   "--model", self.config.model,
                   "--reasoning-effort", self.config.reasoning_effort]

        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=workspace.path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)

        # Send prompt via stdin (avoid argv length limits)
        proc.stdin.write(session_prompt.encode())
        await proc.stdin.drain()
        proc.stdin.close()

        captured_session_id = None
        async for line in proc.stdout:
            event = json.loads(line)
            step = self._parse_event(event)
            if event.get("type") == "session_id":
                captured_session_id = event["session_id"]
            if on_step: await on_step(step)
            if cancel_token.cancelled:
                proc.terminate(); break

        return SessionRunResult(
            agent_session_id=captured_session_id,
            status="completed" if proc.returncode == 0 else "errored")
```

**Features fully leveraged**:
- `codex exec --json` JSONL streaming
- `codex exec resume <id>` for crash recovery
- `--reasoning-effort high|medium|low` per task economic profile
- Stdin prompt (avoids argv length limits)
- Per-step wall-clock timeout via subprocess kill

### 24.3 OpenHands driver

Wraps the OpenHands CLI or its REST server API. Supports `cli` mode (one-shot `openhands --task`) and `server` mode (POST `/api/conversations`, poll events).

```python
class OpenHandsAgent(BaseAgent):
    name = "openhands"

    async def run_session(self, session_prompt, workspace, *, ...):
        if self.mode == "server":
            return await self._run_server(session_prompt, workspace, ...)
        return await self._run_cli(session_prompt, workspace, ...)
```

**Configuration** (`agent.extra`):

| Key | Default | Description |
|---|---|---|
| `mode` | `cli` | `cli` or `server` |
| `cli_bin` | `openhands` | CLI binary name or path |
| `server_url` | `http://localhost:3000` | Used when `mode=server` |
| `agent_cls` | `CodeActAgent` | OpenHands agent class |
| `max_iterations` | 30 | Max agent iterations per session |
| `runtime` | null | Runtime override (e.g. `docker`) |

### 24.4 Custom agent driver

Wraps **any subprocess** as a HorizonX agent. The subprocess receives the session prompt and workspace path; its stdout is streamed back as Steps.

```python
class CustomAgent(BaseAgent):
    name = "custom"

    async def run_session(self, session_prompt, workspace, *, ...):
        # Deliver prompt via stdin / arg / env var / file
        # Stream stdout line-by-line as THOUGHT or JSONL steps
        # Cancel token terminates the subprocess
        ...
```

**Always-injected environment variables**:
- `HORIZONX_WORKSPACE` — absolute path to the session workspace
- `HORIZONX_MODEL` — `agent.model` value
- `HORIZONX_SESSION_ID` — current session identifier

**Configuration** (`agent.extra`):

| Key | Default | Description |
|---|---|---|
| `command` | *required* | Binary to exec (string or list) |
| `prompt_mode` | `stdin` | `stdin` / `arg` / `env` / `file` |
| `output_format` | `text` | `text` (lines→THOUGHT) / `jsonl` (structured Steps) |
| `args` | `[]` | Extra CLI args appended to command |
| `env` | `{}` | Extra environment variables |
| `timeout` | `1800.0` | Per-session hard timeout in seconds |

**JSONL output format** (when `output_format: jsonl`):

```jsonl
{"type": "thought",    "content": {"text": "Planning next step..."}}
{"type": "tool_call",  "tool_name": "Bash", "content": {"command": "pytest tests/"}}
{"type": "observation","tool_name": "Bash", "content": {"output": "5 passed"}}
{"type": "file_change","content": {"path": "src/app.py", "kind": "update"}}
{"type": "error",      "content": {"message": "Build failed"}}
```

Any binary that can accept a task prompt and emit structured lines is a valid HorizonX agent.

---

## 25. Milestone validators — gates not graders

Validators return `Continue / Pause / Abort / RetryWithModification` — not just a 0–1 score.

```python
class Validator(Protocol):
    name: str
    runs: Literal["after_every_session","every_n_sessions","on_demand"]
    async def validate(self, run, session, workspace) -> GateDecision: ...

class GateDecision(BaseModel):
    decision: Literal["continue","pause_for_hitl","abort","retry_with_mod"]
    reason: str
    score: float | None
    details: dict
    suggested_modification: str | None
```

### Built-in validators

| Validator | Use | Anti-gaming guard |
|---|---|---|
| `TestSuiteGate` | pytest, npm test, cargo test | `min_test_count` |
| `PlaywrightGate` | Browser e2e + a11y + console errors | `min_assertions` |
| `LLMProgressGate` | Cheap-model judge: "is progress real?" | LLM-as-judge with rubric |
| `MetricGate` | Python callable, asserts metric ∈ range | Compare to baseline |
| `ShellGate` | Generic shell command exit-code | — |
| `GitGate` | Working tree clean, required commit tags | — |
| `GoalGraphGate` | No orphans, no stuck nodes, structure intact | Hash check |
| `SecurityScanGate` | Bandit / Semgrep, fails on critical | Issue severity threshold |

### Custom validator (30 lines)

```python
class SecurityScanGate(BaseValidator):
    name = "security_scan"
    runs = "after_every_session"

    async def validate(self, run, session, workspace) -> GateDecision:
        result = await workspace.run("bandit -r src/ -f json")
        issues = json.loads(result.stdout)
        critical = [i for i in issues if i["issue_severity"] == "HIGH"]

        if not critical:
            return GateDecision(decision="continue", reason="no critical issues",
                                score=1.0, details={"issues": []})

        return GateDecision(
            decision="pause_for_hitl",
            reason=f"{len(critical)} critical security issues",
            score=0.0,
            details={"critical_issues": critical[:5]},
            suggested_modification="fix critical issues; consider Semgrep auto-fix")
```

---

## 26. Spin detection — multi-layer anti-cycling

Spin detection is HorizonX's most distinctive feature. Six layers run in sequence; the first to fire terminates the check and triggers recovery.

```python
class SpinDetector:
    layers = [
        ExactLoopLayer(hard_threshold=3, soft_threshold=2, window=20),
        EditRevertLayer(),
        ScorePlateauLayer(window=3, delta=0.02),
        ToolThrashingLayer(),
        BucketedHashLayer(soft_threshold=3, hard_threshold=5, window=30),
        SemanticProgressLayer(model="claude-haiku-4-5", every_n=20),
    ]

    async def check(self, session) -> SpinReport:
        for layer in self.layers:
            r = await layer.check(session)
            if r.detected: return r
        return SpinReport(detected=False)
```

### 26.1 Layer 1 — Exact loop (cheap, deterministic)

Hash each tool call as `(tool_name, normalized_args)`. If same hash appears N times in last K steps → fire.

```python
class ExactLoopLayer:
    def __init__(self, threshold=3, window=20):
        self.threshold, self.window = threshold, window

    async def check(self, session) -> SpinReport:
        recent = await session.recent_steps(self.window)
        hashes = [hash_step(s) for s in recent if s.type == "TOOL_CALL"]
        counts = Counter(hashes)
        offenders = [h for h, c in counts.items() if c >= self.threshold]
        if offenders:
            return SpinReport(detected=True, layer="exact_loop",
                              detail={"hash": offenders[0], "count": counts[offenders[0]]},
                              action="terminate_session_and_retry")
        return SpinReport(detected=False)
```

### 26.2 Layer 2 — Edit-revert (file diff oscillation)

Track per-file diff history. If a file is modified to state A, then to state B, then back to A → fire.

```python
class EditRevertLayer:
    async def check(self, session) -> SpinReport:
        diffs = await session.file_diff_history()
        for path, history in diffs.items():
            if len(history) >= 4:
                hashes = [h.content_hash for h in history[-4:]]
                if hashes[0] == hashes[2] and hashes[1] == hashes[3]:
                    return SpinReport(detected=True, layer="edit_revert",
                                      detail={"path": path, "history": hashes},
                                      action="terminate_and_hitl")
        return SpinReport(detected=False)
```

### 26.3 Layer 3 — Score plateau (metric stagnation)

Across last K validator runs, slope of score < δ → fire.

```python
class ScorePlateauLayer:
    def __init__(self, window=3, delta=0.02):
        self.window, self.delta = window, delta

    async def check(self, session) -> SpinReport:
        scores = await session.recent_validator_scores(self.window)
        if len(scores) < self.window: return SpinReport(detected=False)
        slope = linregress(range(len(scores)), scores).slope
        if abs(slope) < self.delta:
            return SpinReport(detected=True, layer="score_plateau",
                              detail={"slope": slope, "scores": scores},
                              action="switch_strategy_or_hitl")
        return SpinReport(detected=False)
```

### 26.4 Layer 4 — Tool thrashing

Same tool with contradictory args (`enable=True` then `enable=False`) within K steps, or one tool used exclusively for >80% of the last 20 calls.

### 26.5 Layer 5 — Bucketed hash (fuzzy repetition)

Tolerates minor argument variation that exact hashing would miss. Buckets tool calls by `(tool_name, normalized_arg_shape)` — e.g. different filenames in the same `Read` pattern are the same bucket. Dual-threshold: soft fires a warning at `soft_threshold` hits, hard terminates at `hard_threshold`.

```python
class BucketedHashLayer:
    def __init__(self, soft_threshold=3, hard_threshold=5, window=30):
        ...
    async def check(self, session) -> SpinReport:
        recent = await session.recent_steps(self.window)
        tool_steps = [s for s in recent if s.type == StepType.TOOL_CALL]
        buckets = Counter(self._bucket_key(s) for s in tool_steps)
        max_count = max(buckets.values(), default=0)
        if max_count >= self.hard_threshold:
            return SpinReport(detected=True, layer="bucketed_hash",
                              detail={"tier": "hard", "count": max_count},
                              action="terminate_session_and_retry")
        if max_count >= self.soft_threshold:
            return SpinReport(detected=True, layer="bucketed_hash",
                              detail={"tier": "soft", "count": max_count},
                              action="warn_and_inject_diagnostic")
        return SpinReport(detected=False)
```

### 26.6 Layer 6 — Semantic progress (LLM-as-judge)

Every N steps, ask cheap model: *"is this agent making progress on the stated sub-goal?"* Fires only on confident "no." Most expensive layer — runs last and only at intervals.

### Dual-threshold design (Layers 1 and 5)

Layers 1 (ExactLoop) and 5 (BucketedHash) use a **soft / hard** threshold pair:

| Threshold | Count | Action |
|---|---|---|
| Soft | `soft_threshold` | `warn_and_inject_diagnostic` — inject a nudge, continue session |
| Hard | `hard_threshold` | `terminate_session_and_retry` — close session, start fresh |

This avoids false positives (some tools are legitimately called twice) while still catching genuine loops.

### Recovery actions

| Layer fired | Default action | Why |
|---|---|---|
| Exact loop (soft) | `warn_and_inject_diagnostic` | Might self-correct with a nudge |
| Exact loop (hard) | `terminate_and_retry` | Context issue; fresh session helps |
| Edit-revert | `terminate_and_hitl` | Indicates real ambiguity |
| Score plateau | `switch_strategy_or_hitl` | Strategy may be wrong |
| Tool thrashing | `warn_and_inject_diagnostic` | Often self-correctable |
| Bucketed hash (soft) | `warn_and_inject_diagnostic` | Minor variation, not a hard loop yet |
| Bucketed hash (hard) | `terminate_and_retry` | Fuzzy loop confirmed |
| Semantic | `terminate_and_re_decompose` | Goal too big or unclear |

---

## 27. Retry strategies

Multiple retry modes, picked by failure mode.

| Retry mode | Trigger | Mechanism |
|---|---|---|
| **Naive** | Transient (timeout, network) | Same prompt, fresh session |
| **Mutation** | Semantic failure | Same goal, append failure context + alternate-approach hint |
| **Decomposition** | Sub-goal too big | Spawn re-decomposer to split into smaller leaves |
| **Escalation** | Capability ceiling | Switch to more capable model (Sonnet → Opus) |
| **Strategy switch** | Wrong strategy | Sequential → DecompositionFirst, or Ralph → Tree |

```python
class RetryEngine:
    async def retry(self, goal: GoalNode, last_failure: FailureReport) -> RetryPlan:
        if last_failure.reason == "transient":
            return RetryPlan(mode="naive")
        if last_failure.reason == "semantic" and goal.attempts < 2:
            return RetryPlan(mode="mutation",
                             hint=self._extract_alt_approach(last_failure))
        if last_failure.reason == "too_big" or goal.attempts >= 2:
            return RetryPlan(mode="decomposition")
        if last_failure.reason == "capability":
            return RetryPlan(mode="escalation",
                             new_model=self._next_tier(goal))
        return RetryPlan(mode="hitl")
```

---

## 28. Early stopping

Configurable predicates that abort a run cleanly.

```python
class EarlyStopPredicate(Protocol):
    async def should_stop(self, run: Run) -> StopDecision: ...

# Built-ins
class MetricPlateau:    # last K metrics, slope < δ
class BudgetThreshold:  # tokens/cost/time hit X% of cap
class HITLSignal:       # operator clicked stop
class DiminishingReturns:  # last K improvements < 0.5 × historical avg
class ConfidenceFloor:  # rolling validator score below threshold
```

Early stop is **not** spin — it's "we're done getting useful work; stop spending."

---

# Part V — Operations

The four cross-cutting capabilities that make a harness production-grade.

## 29. Auditability

Every step, every decision, every state change persists with timestamp, actor, and rationale. Months later you can answer: *what did this agent do? why? on what evidence?*

### Append-only stores
- `decisions.jsonl` — agent's stated reasons for non-trivial choices
- `failures.jsonl` — every attempt that didn't work + observed outcome
- `steps` table — every event ever, immutable
- `validations` table — every gate decision with reason
- `hitl_events` table — every human intervention with operator + instruction
- `spin_reports` table — every layer firing
- Git history of the workspace — every code change with descriptive commit

### Audit queries
```sql
-- Why did this run pause for HITL?
SELECT trigger, context, decision, instruction
FROM hitl_events WHERE run_id = ?;

-- What did this sub-goal try?
SELECT s.sequence_index, st.tool_name, st.content, st.timestamp
FROM steps st JOIN sessions s ON st.session_id = s.id
WHERE s.run_id = ? AND s.target_goal_id = ?
ORDER BY s.sequence_index, st.sequence;

-- What approaches failed across all runs for this task type?
SELECT goal_id, attempt, outcome FROM failures_log
WHERE task_kind = 'oauth_implementation';
```

### Reproducibility
Every run snapshots its full `Task` config in `runs.task_snapshot`. You can reconstruct exactly what was attempted, with what config, against what baseline workspace (git tag).

### Compliance
The audit trail is sufficient for SOC2-style "who did what when" attestation. Every action has: `run_id · session_id · goal_id · agent_session_id · operator (if HITL) · timestamp · rationale`.

---

## 30. Forkable runs

`rt.fork_run(parent, mutation)` creates a child run that **inherits** the parent's workspace at a snapshot, runs independently, and either merges back or stays diverged.

### Why fork?
- **`TreeOfTrials`** — N parallel trials with mutated configs, best wins
- **A/B prompts** — try two prompts on the same task, compare
- **Speculative execution** — explore a risky path without committing
- **Bisection** — fork from a known-good point, isolate when things broke

### How fork works

```python
async def fork_run(self, parent: Run, mutation: dict) -> Run:
    # 1. Snapshot parent workspace (git stash + branch + clone)
    workspace = await self._snapshot_workspace(parent.workspace_path)

    # 2. Copy handoff files (progress.md, goals.json, decisions.jsonl, summary.md)
    await self._copy_handoff_files(parent.workspace_path, workspace)

    # 3. Create child Run row, parent_run_id = parent.id
    child_task = self._apply_mutation(parent.task, mutation)
    child = Run(id=uuid4(), parent_run_id=parent.id, task=child_task,
                workspace_path=workspace, ...)
    await self.store.insert_run(child)

    # 4. Inherit goal graph (same nodes, fresh attempts/notes)
    await self._inherit_goal_graph(parent.id, child.id)

    # 5. Emit fork event
    await self.bus.publish(ForkCreatedEvent(parent.id, child.id, mutation))
    return child
```

### Merge

```python
async def merge_run(self, parent: Run, child: Run) -> None:
    # 1. Goal graph: 3-way merge (common ancestor + both sides)
    await self._merge_goal_graphs(parent.id, child.id)

    # 2. progress.md: chronological append with branch tags
    await self._append_progress_with_tag(parent, child)

    # 3. decisions.jsonl / failures.jsonl: union (failures ∪)
    await self._union_jsonl(parent, child, ["decisions.jsonl","failures.jsonl"])

    # 4. Workspace: git merge with conflict resolution (or HITL)
    result = await self._git_merge(parent.workspace_path, child.workspace_path)
    if result.has_conflicts:
        await self.request_hitl(parent, "merge_conflict", context=result)

    await self.bus.publish(ForkMergedEvent(parent.id, child.id))
```

### Mutation kinds (for fork)
- **prompt** — same task, different framing
- **model** — Opus vs Sonnet vs Haiku
- **strategy** — Sequential vs Ralph vs PairProgramming
- **decomposition** — different goal-graph structures from same root
- **tool-allowlist** — different action spaces
- **temperature / reasoning_effort** — same model, different settings

---

## 31. Retryability

Retry is built into every layer. Sub-goal retries (§27), session retries (transient errors), validator-failure retries (`retry_with_mod`).

### Retry config

```yaml
retry:
  per_subgoal:
    max_attempts: 3
    on_attempt_2: { mode: mutation }
    on_attempt_3: { mode: decomposition }
    on_max_exceeded: { mode: hitl }
  per_session:
    transient_max: 2          # network/timeout
    backoff: exponential
  per_validator:
    after_retry_with_mod: 1   # validator can suggest one mod
```

### Retry observability
Every retry emits a `retry.attempted` event with `mode`, `previous_failure`, `expected_outcome`. Easy to query: *"which goals required mutation retries?"* — that's a goal-graph quality signal for your prompt template.

---

## 32. HITL gates

Some moments need humans. HorizonX surfaces them and waits.

### Triggers (configurable)
- Spin detected (2+ layers fired or critical layer)
- Milestone validator returned `pause_for_hitl`
- Resource budget passes a threshold (50%, 75%, 90%)
- Sub-goal failed N consecutive attempts
- Agent explicitly requests HITL via tool call
- Operator-initiated pause

### HITL flow
1. Run status → `PAUSED_HITL`
2. Notification fires (Slack, email, webhook)
3. Operator gets a link to the dashboard with full context
4. Operator can: `approve` · `modify` · `abort` · `re-decompose`
5. Decision recorded as a `Step` of type `HITL_DECISION` in `hitl_events`
6. Run resumes with operator's instruction injected into next session prompt

### HITL config

```yaml
hitl:
  triggers:
    - spin_detected
    - validator_paused
    - budget_threshold: 75
    - subgoal_max_attempts
  notification:
    type: slack
    channel: "#horizonx-alerts"
    severity_routing:
      critical: "@oncall"
      normal: "channel"
  context_payload:
    include_summary: true
    include_recent_progress: 50    # last 50 progress.md lines
    include_failures_for_goal: true
    include_dashboard_link: true
  resume_protocol:
    inject_operator_instruction: true
    require_acknowledgement: false
```

---

## 33. Observability and live monitoring

A long-horizon run is autonomous, but the operator must never be in the dark.

### Surfaces
- **Terminal**: `horizonx watch <run_id>` — Rich live UI with current goal, recent steps, validator status, budget
- **Web dashboard**: optional FastAPI + SSE app, `/runs/<id>` shows live trajectory + goal graph viz
- **Slack/Discord**: configurable alerts on key events
- **Webhooks**: arbitrary HTTP POST per event for custom integration
- **CLI queries**: `horizonx show <run_id>`, `horizonx compare <a> <b>`, `horizonx export <id>`

### Health metrics emitted
- Goals: `total · pending · in_progress · done · failed · blocked`
- Sessions: `count · avg_duration · timeout_rate · spin_rate`
- Validators: `pass_rate · avg_duration · pause_rate`
- Budget: `tokens_used / max · usd_used / max · seconds / max`
- Spin: `layer_fire_rate` per layer
- HITL: `pause_count · avg_resolution_minutes · operator_actions`

### Real-time event stream
Every state change emits an `Event` to the bus. The terminal `watch` UI is a thin SSE consumer; the web dashboard is the same. Building a custom dashboard takes ~50 lines.

---

# Part VI — Use cases

Each section below is a real long-horizon task type, showing **goal graph shape, strategy choice, validator stack, expected duration, and failure modes addressed.**

## 34. Use case 1 — Coding (build OAuth)

**Task**: Implement OAuth 2.0 Authorization Code Flow with PKCE in an existing FastAPI app.

```yaml
id: build-oauth-001
horizon_class: very_long
estimated_duration_hours: [4, 12]

execution:
  strategy: sequential_sub_goals
  initializer:
    decompose_to_goal_graph: true
    target_subgoals: [40, 80]
  per_session:
    max_steps: 50
    max_minutes: 25

agent:
  type: claude_code
  model: claude-opus-4-7
  thinking_budget: 10000
  allowed_tools: [Read, Edit, Bash, Glob, Grep]

milestone_validators:
  - id: tests_pass
    type: test_suite
    runs: after_every_session
    command: pytest tests/ -k oauth --tb=short
    on_fail: pause_for_hitl
  - id: build_works
    type: shell
    command: uvicorn app.main:app --host 0.0.0.0 --port 0
    on_fail: retry_with_modification
  - id: security_scan
    type: shell
    command: bandit -r app/oauth -f json
    on_fail: pause_for_hitl

resources:
  max_total_hours: 12
  max_total_tokens: 5_000_000
  max_total_usd: 50
```

**Goal graph shape**: 4 level-1 goals (auth, token, tests, docs) → 12 level-2 leaves → ~50 atomic actions.

**Failure modes addressed**: 1, 2, 6, 7, 11, 12 (test deletion, premature completion, brittle handoffs).

---

## 35. Use case 2 — ML training (Karpathy autoresearch wrapped)

**Task**: Wrap [Karpathy's autoresearch](https://github.com/karpathy/autoresearch) — autonomous overnight LLM hyperparameter exploration. Use Ralph loop with 5-min training cycles.

```yaml
id: ml-autoresearch-001
horizon_class: very_long
estimated_duration_hours: [8, 16]

execution:
  strategy: ralph_loop
  iteration:
    fixed_minutes_per_iter: 5
    total_minutes: 600                # ~120 iters overnight
  mutable_paths: ["train.py"]         # ONLY this file may change
  metric:
    name: val_bpb
    direction: minimize
    measurement: "uv run train.py | grep val_bpb"

agent:
  type: claude_code
  model: claude-opus-4-7

milestone_validators:
  - id: foreign_files_unchanged
    type: shell
    command: "git diff --name-only HEAD~ HEAD | grep -v ^train.py$ && exit 1 || exit 0"
    runs: after_every_session
    on_fail: abort
  - id: train_runs
    type: shell
    command: "timeout 360 uv run train.py"
    runs: after_every_session
    on_fail: discard_iteration

early_stopping:
  metric_plateau:
    window: 10
    delta: 0.001
```

**Goal graph shape**: simple — root + N iteration nodes (the metric history is the plan).

**Failure modes addressed**: 3, 4, 7, 8, 9 (cyclic loops, edit-revert, stagnation, crash, cost runaway).

---

## 36. Use case 3 — SRE monitoring

**Task**: Long-lived agent that watches Prometheus alerts, triages, attempts auto-remediation, escalates on uncertainty.

```yaml
id: sre-monitor-001
horizon_class: continuous
estimated_duration_hours: continuous

execution:
  strategy: monitor_respond
  signal_source:
    type: prometheus
    alert_rules_path: prometheus/alerts.yaml
    poll_seconds: 30
  trigger_filter:
    severity: [warning, critical]
    namespace: [production]

agent:
  type: claude_code
  model: claude-sonnet-4-6              # cheaper for routine triage
  allowed_tools: [Read, Bash, WebSearch]

milestone_validators:
  - id: change_safe
    type: llm_judge
    runs: before_destructive_action
    rubric: "Is this kubectl/aws action reversible and SLO-positive?"
    on_fail: pause_for_hitl
  - id: alert_resolved
    type: shell
    command: "promtool query instant 'ALERTS{...}' | jq .data.result | wc -l"
    runs: after_every_session
    threshold: 0
    on_fail: continue                    # stays in monitor mode

hitl:
  triggers:
    - validator_paused
    - production_namespace_modification
    - subgoal_max_attempts
  notification:
    type: slack
    channel: "#sre-oncall"
    severity_routing: { critical: "@oncall" }
```

**Goal graph shape**: root + sub-goal per incident; grows over time. Each incident's full trajectory persists as the runbook.

**Failure modes addressed**: 8, 9, 10, 14 (crash recovery, cost, blindness, permission creep).

---

## 37. Use case 4 — Complex decision (M&A due diligence)

**Task**: Investigate a target company across financial, legal, technical, and cultural dimensions. Synthesize findings with claim-evidence pairs.

```yaml
id: ma-diligence-001
horizon_class: long
estimated_duration_hours: [6, 12]

execution:
  strategy: decomposition_first       # plan → HITL approve → execute
  inner:
    strategy: sequential_sub_goals
    per_session: { max_steps: 80, max_minutes: 40 }

agent:
  type: claude_code
  model: claude-opus-4-7
  thinking_budget: 20000              # high reasoning
  allowed_tools: [Read, Write, WebSearch, WebFetch]

milestone_validators:
  - id: claims_have_evidence
    type: llm_judge
    runs: every_n_sessions
    n: 3
    rubric: "Does every non-trivial claim cite a source? List unsupported claims."
    on_fail: pause_for_hitl
  - id: scope_coverage
    type: shell
    command: "python check_coverage.py --required financial,legal,tech,culture"
    runs: every_n_sessions
    n: 5
    on_fail: continue                 # signals what's incomplete
  - id: contradictions
    type: llm_judge
    runs: every_n_sessions
    n: 5
    rubric: "Identify contradictions in the synthesized findings."
    on_fail: pause_for_hitl

hitl:
  triggers:
    - plan_review                     # mandatory after decomposition
    - validator_paused
```

**Goal graph shape**: deep — 4 dimensions × 5–10 investigation areas × 3–5 sub-questions = ~80 leaves.

**Failure modes addressed**: 1, 2, 5, 7, 11, 14 (context exhaustion, plan drift, silent stagnation, permission creep).

---

## 38. Use case 5 — Migration (monolith → microservices)

**Task**: Migrate 50 services from monolith to microservices on Kubernetes.

```yaml
id: migration-msvc-001
horizon_class: very_long
estimated_duration_hours: [40, 200]   # days

execution:
  strategy: composite
  pipeline:
    - DecompositionFirst:
        inner: { strategy: sequential_sub_goals }
    - SequentialSubgoals:
        per_session: { max_steps: 60 }

agent:
  type: codex
  model: o3
  reasoning_effort: high

milestone_validators:
  - id: per_service_smoke
    type: shell
    command: "./smoke.sh ${service_name}"
    runs: after_every_session
    on_fail: pause_for_hitl
  - id: rollback_works
    type: shell
    command: "./rollback_test.sh ${service_name}"
    runs: after_every_session
    on_fail: abort                     # never accept a non-rollbackable migration
  - id: monolith_intact
    type: shell
    command: "pytest monolith/tests/ -x"
    runs: after_every_session
    on_fail: pause_for_hitl

retry:
  per_subgoal:
    max_attempts: 2
    on_attempt_2: { mode: mutation }
    on_max_exceeded: { mode: hitl }

hitl:
  triggers:
    - validator_paused
    - production_deployment            # always HITL on prod deploys
    - subgoal_max_attempts
```

**Goal graph shape**: root → 50 service nodes (level 1) → per-service [extract, package, deploy, smoke, cutover] (level 2).

**Failure modes addressed**: 5, 8, 11, 14 (drift, crash, brittle handoffs, permission creep).

---

## 39. Use case 6 — Content (technical book)

**Task**: Write a 50-page technical guide on Kubernetes operators with code examples.

```yaml
id: content-book-001
horizon_class: very_long
estimated_duration_hours: [16, 40]

execution:
  strategy: pair_programming
  builder: { type: claude_code, model: claude-opus-4-7 }
  critic:  { type: codex,        model: o3, reasoning_effort: high }

milestone_validators:
  - id: code_compiles
    type: shell
    command: "for f in chapters/*/code/; do (cd $f && go build ./...); done"
    runs: after_every_session
  - id: factual_accuracy
    type: llm_judge
    runs: every_n_sessions
    n: 3
    rubric: "Identify factual claims and verify against source. Flag unverified."
  - id: readability
    type: shell
    command: "python check_readability.py chapters/ --min-flesch 60"
    runs: every_n_sessions
    n: 5
```

**Goal graph shape**: book → chapters (5–10) → sections → paragraphs.

**Failure modes addressed**: 5, 7, 11, 12 (drift, stagnation, brittle handoffs, validation theater).

---

## 40. Use case 7 — Research synthesis

**Task**: Synthesize a comprehensive review of 100 papers on a topic. Maintain coherence and find gaps.

```yaml
id: research-synth-001
horizon_class: long
estimated_duration_hours: [4, 10]

execution:
  strategy: composite
  pipeline:
    - SequentialSubgoals: {}            # read each paper
    - TreeOfTrials:                     # diverse synthesis approaches
        rounds: 1
        trials_per_round: 3
        mutations: [prompt, model]
    - SequentialSubgoals: {}            # final review pass

agent: { type: claude_code, model: claude-opus-4-7 }

milestone_validators:
  - id: papers_covered
    type: shell
    command: "python check_papers.py --required-min 100"
    runs: every_n_sessions
  - id: coherence
    type: llm_judge
    rubric: "Do the synthesized claims form a coherent narrative? Cite contradictions."
    runs: every_n_sessions
    n: 5
```

---

# Part VII — Implementation

## 41. Skeleton code

The minimum viable skeleton, ready for fill-in. Approximately the structure of `horizonx/`:

```
horizonx/
├── __init__.py
├── core/
│   ├── types.py           # Task, Run, Session, Step, GoalNode (§10)
│   ├── runtime.py         # Runtime class (§11)
│   ├── goal_graph.py      # GoalGraph operations (§12)
│   ├── session_manager.py # SessionManager (§13)
│   ├── recorder.py        # TrajectoryRecorder (§16)
│   ├── summarizer.py      # Summarizer (§15)
│   ├── spin_detector.py   # SpinDetector + 5 layers (§26)
│   ├── retry_engine.py    # RetryEngine (§27)
│   ├── early_stop.py      # EarlyStop predicates (§28)
│   ├── governor.py        # ResourceGovernor (§19)
│   └── event_bus.py       # EventBus (§18)
├── agents/
│   ├── base.py
│   ├── claude_code.py     # §24.1
│   ├── codex.py           # §24.2
│   ├── openhands.py       # §24.3
│   ├── custom.py          # §24.4 — any subprocess as an agent
│   ├── mock.py            # deterministic driver for tests
│   └── registry.py
├── strategies/
│   ├── single.py          # §21.1
│   ├── sequential.py      # §21.2
│   ├── ralph.py           # §21.3
│   ├── tree.py            # §21.4
│   ├── monitor.py         # §21.5
│   ├── decomposition.py   # §21.6
│   ├── pair.py            # §21.7
│   └── self_critique.py   # §21.8
├── validators/
│   ├── base.py
│   ├── test_suite.py
│   ├── playwright.py
│   ├── llm_judge.py
│   ├── metric.py
│   ├── shell.py
│   ├── git.py
│   └── goal_graph.py
├── environments/
│   ├── podman.py
│   ├── docker.py
│   └── local.py
├── storage/
│   ├── models.py          # SQLAlchemy models matching §17
│   ├── sqlite.py
│   └── postgres.py
├── monitoring/
│   ├── server.py          # FastAPI + SSE
│   ├── ws.py              # WebSocket events
│   └── slack.py           # Slack notifications
├── hitl/
│   ├── gate.py
│   └── resume.py
├── operations/
│   ├── audit.py           # §29
│   ├── fork.py            # §30
│   └── retry.py           # §31
└── cli.py                 # `horizonx run`, `watch`, `show`, `fork`, `compare`
```

### Minimal CLI

```bash
horizonx run examples/coding_oauth/ --agent claude-code --strategy sequential
horizonx run --config configs/my_task.yaml --resume run-abc123
horizonx watch run-abc123
horizonx show run-abc123
horizonx fork run-abc123 --mutation '{"agent.model":"claude-sonnet-4-6"}'
horizonx merge run-abc123 run-fork-xyz
horizonx compare run-abc123 run-def456
horizonx export run-abc123 --format json > run.json
horizonx serve --port 8080                    # web dashboard
```

### Minimal Python SDK usage

```python
from horizonx import Task, Runtime
from horizonx.strategies import SequentialSubgoals
from horizonx.agents import ClaudeCodeAgent
from horizonx.validators import TestSuiteGate, LLMProgressGate

task = Task(
    id="build-oauth-001",
    name="Implement OAuth 2.0",
    prompt=open("prompts/oauth.md").read(),
    horizon_class="very_long",
    strategy=SequentialSubgoals(target_subgoals=(40, 80), per_session_max_steps=50),
    agent=ClaudeCodeAgent(model="claude-opus-4-7", thinking_budget=10000,
                          allowed_tools=["Read","Edit","Bash","Glob","Grep"]),
    milestone_validators=[
        TestSuiteGate(runs="after_every_session", command="pytest tests/"),
        LLMProgressGate(runs_every_n=5, model="claude-haiku-4-5"),
    ],
    handoff_files=["progress.md","goals.json","decisions.jsonl","failures.jsonl"],
    resources={"max_hours": 12, "max_tokens": 5_000_000, "max_usd": 50},
)

runtime = Runtime(db_url="sqlite:///horizonx.db")
report = await runtime.run(task)
```

---

## 42. Implementation status and roadmap

### Implemented ✅

| Component | Notes |
|---|---|
| `core/types.py` — Task, Run, Session, Step, GoalNode | Full Pydantic v2 schema |
| `core/runtime.py` — orchestrator with fork/merge | Primitives: start/end session, record step, run validators |
| `core/goal_graph.py` — versioned DAG | Cycle detection, dependency ordering, status propagation |
| `core/event_bus.py` — in-memory pub/sub | Swap Redis/NATS for multi-process |
| `core/summarizer.py` — context compression | Structured output, Haiku model by default |
| `core/spin_detector.py` — 6 layers | ExactLoop (dual-threshold), EditRevert, ScorePlateau, ToolThrashing, BucketedHash, Semantic |
| `core/governor.py` — resource limits | Wall-clock, tokens, USD, per-session caps, stall watchdog |
| `storage/sqlite.py` — durable store | aiosqlite async, all tables from §17 |
| `agents/claude_code.py` — full driver | stream-json, thinking, MCP, session resume |
| `agents/codex.py` — full driver | JSONL stream, reasoning-effort, stdin prompt, resume |
| `agents/openhands.py` — full driver | CLI + server mode, event streaming |
| `agents/custom.py` — subprocess wrapper | 4 prompt modes, text/jsonl output, cancel, timeout |
| All 8 strategies | single, sequential, ralph, tree, monitor, decomposition, pair, self_critique |
| All 6 validators | test_suite, shell, llm_judge, metric, git, goal_graph |
| `hitl/gate.py` | Pause/resume, decision routing |
| `runtime/watchdog.py` | Stall watchdog with soft nudge → hard abort |
| `agents/repair.py` | Dangling tool-call repair |
| `cli.py` | `run`, `watch`, `show`, `list`, `fork`, `serve` |
| 8 example tasks | One per strategy, all runnable |
| `examples/master_template.yaml` | Every config option annotated with defaults |
| 222 tests, all passing | Module-aligned test files |

### In progress 🔧

- Web dashboard (FastAPI + SSE) — server scaffolded, UI in progress
- PostgreSQL backend — DDL ready, driver pending

### Planned 📋

- Multi-process / distributed runs (Redis event bus)
- Cost dashboard with live model pricing
- Playwright e2e validator
- Plugin entry-points for third-party agents/validators (pyproject entry-points already wired for first-party)
- `composite` strategy — pipeline multiple strategies in sequence

---

## 43. References

### Primary inspirations
- Anthropic — [*Effective harnesses for long-running agents*](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) — two-agent + feature-list pattern; *"compaction isn't sufficient"*; the `passes` field discipline; mandatory session checklists
- Karpathy — [*autoresearch*](https://github.com/karpathy/autoresearch) — Ralph loop pattern; mutable surface vs immutable infrastructure; time-boxed iterations; metric-driven retention

### Eval harnesses (for trajectory schema and reliability metrics)
- [SWE-bench](https://www.swebench.com/) — 3-tier Docker isolation; PR-based leaderboard submission
- [τ-bench](https://github.com/sierra-research/tau-bench) — `pass^k` as the reliability metric
- [Inspect AI (UK AISI)](https://inspect.aisi.org.uk/) — sandboxing primitives
- [AgentBench](https://github.com/THUDM/AgentBench), [WebArena](https://github.com/web-arena-x/webarena), [OSWorld](https://github.com/xlang-ai/OSWorld) — task-domain coverage

### Workflow / durability concepts (for the production-runtime mental model)
- [Temporal.io](https://temporal.io/) — durable execution; HorizonX is the agent-shaped analog
- [Airflow](https://airflow.apache.org/), [Prefect](https://www.prefect.io/) — DAG runners with retry/recovery

### Adjacent agent frameworks (composable, not competitors)
- [LangGraph](https://www.langchain.com/langgraph) — useful inside an agent, not for orchestration
- [OpenHands (formerly OpenDevin)](https://github.com/All-Hands-AI/OpenHands) — alternative agent driver target
