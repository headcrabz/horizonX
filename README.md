# HorizonX

> **Temporal/Airflow for long-horizon agents.** A pluggable execution framework that runs Claude Code, Codex, OpenHands, and custom agents reliably for hours and days — with goal tracking, milestone validation, spin detection, checkpoint/resume, HITL gates, and real-time observability.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Tests: 206 passing](https://img.shields.io/badge/tests-206%20passing-brightgreen.svg)](tests/)
[![Status: Alpha](https://img.shields.io/badge/status-alpha-yellow.svg)](https://github.com/)

---

## Why HorizonX exists

Frontier models can plan and reason. They struggle to **execute reliably for hours**. Anthropic's own engineering team says it plainly:

> *"Out of the box, even a frontier coding model like Opus 4.5 running on the Claude Agent SDK in a loop across multiple context windows will fall short."*
>
> — Anthropic, [*Effective harnesses for long-running agents*](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)

The model isn't the bottleneck. **The harness is.** Long-horizon execution requires infrastructure the model itself cannot provide:

- A **persistent goal graph** so the agent doesn't lose the plot at context-window boundaries
- A **checkpoint protocol** so a 4-hour task doesn't restart from zero on a crash
- **Milestone validators** that gate progress instead of just scoring the end
- **Spin detection** so an agent that's looping gets terminated, not paid for
- **Handoff artifacts** (progress.md, goals.json, decisions.jsonl) that survive between sessions
- **Real-time observability** so an operator can see what's happening at hour 3 of 8
- **Pluggable execution strategies** — sequential sub-goals, Ralph loops, tree-of-trials, monitor-respond

HorizonX is **none of an agent itself**. It's the runtime that makes whatever agent you bring (Claude Code, Codex CLI, OpenHands, your own) actually finish a long job.

---

## Mental model

The real question isn't "SWE-bench or HorizonX" — those are orthogonal concerns. The question every developer hits is:

> **"Why can't I just call Claude Code in a loop myself?"**

You can. Until hour 3, when it spins for 40 minutes without progress. Or hour 6, when it declares the task done but broke three tests. Or when the harness crashes and you restart from zero.

| | Roll your own loop | LangGraph / CrewAI | **HorizonX** |
|---|---|---|---|
| Crash recovery | Restart from zero | Partial / framework-specific | Resume from last commit + handoff |
| Spin detection | None | None | 6-layer detector with dual thresholds |
| Context exhaustion | Manual | Manual | Auto-summarize → fresh session with handoff |
| Milestone gates | None | None | Typed validators, declared in YAML |
| Execution strategies | One loop | Via DSL | 8 first-class strategies, per-task in YAML |
| Agent-agnostic | N/A | Mostly LLM API | Claude Code, Codex, OpenHands, any subprocess |
| Goal persistence | None | None | Durable goal graph survives crashes + session gaps |
| Premature completion | Common | Common | Prevented by construction — runtime owns state transitions |

> *If you can write a 50-line async loop, you can start a long job. HorizonX is what you wish you had at hour 6.*

---

## What's novel

HorizonX makes several contributions that don't exist as a unified system elsewhere:

**1. The session boundary is the primitive**
Agent frameworks operate within one context window. Workflow engines orchestrate deterministic functions. HorizonX orchestrates across *stochastic session resets* — where context evaporates, agents drift, and crashes happen mid-goal. It makes those boundaries survivable: durable goal graph, structured handoffs, validator-gated transitions. Nothing else treats the session boundary as the unit of execution.

**2. A 14-mode failure taxonomy for long-horizon agents**
Premature completion · cyclic loops · edit-revert oscillation · plan drift · test deletion · validation theater · context exhaustion · brittle handoffs · operator blindness · cost runaway · silent stagnation · crash-equals-loss · tool overuse · permission creep. Each failure mode has a structural mitigation in the harness — not a prompt instruction.

**3. 6-layer spin detection — defense-in-depth**
No other agent framework ships structured spin detection. HorizonX stacks six independent detectors — exact tool-call repetition, file edit-revert oscillation, validator score plateau, semantic LLM-judge progress check, bucketed fuzzy hash, and tool-thrashing distribution — each with a soft-warn and hard-abort threshold. Catching loops the agent can't catch itself is a load-bearing capability for any multi-hour run.

**4. Execution strategy as a task parameter, not a framework assumption**
Every existing long-horizon system hardcodes the execution loop. HorizonX is the first where the strategy (`sequential`, `ralph`, `tree`, `pair`, `self_critique`, ...) is a per-task YAML field. A security audit should run 3 parallel hardening attempts. A refactor should iterate with rollback. An API design should decompose into a sub-goal plan first. These are not the same loop.

**5. Validators-as-gates, not graders**
Eval harnesses produce scores. HorizonX validators produce decisions: `Continue · Pause-for-HITL · Abort · Retry-with-modification`. Production execution needs decisions, not score distributions. This is a typed `GateAction` enum — every validator must pick one.

**6. Agent-proposes, runtime-accepts goal state transitions**
The agent can only propose a goal as done. The runtime accepts only after validators confirm. Premature completion — the most common long-horizon failure — is prevented by construction, not by prompt discipline.

**7. `failures.jsonl` as negative epistemic memory**
`progress.md` tracks what was done. `failures.jsonl` tracks what didn't work and why. Every session injects both into the next session's prompt. Agents stop re-trying broken approaches across sessions — a failure mode endemic to all single-file handoff designs.

**8. Forkable runs with goal-graph merge**
TreeOfTrials forks the run into N independent workspace branches, scores each, prunes below threshold, and merges the winner. One human reviews one clean diff — not N half-baked PRs. The goal graph merge is 3-way: DAG union, `failures.jsonl` union, `progress.md` chronological merge.

---

## What HorizonX does — and what you bring

```
┌─────────────────────────────────────────────────────────────────┐
│                          You provide                            │
├─────────────────────────────────────────────────────────────────┤
│  • Task spec (YAML or Python)                                   │
│  • Validators (Python callables — gates, not graders)           │
│  • Goal decomposition (or let the initializer do it)            │
│  • Success criteria & resource limits                           │
└─────────────────────────────────────────────────────────────────┘
                                ↓
┌─────────────────────────────────────────────────────────────────┐
│                       HorizonX handles                          │
├─────────────────────────────────────────────────────────────────┤
│  • Reliable agent invocation (Claude Code / Codex / Custom)     │
│  • Goal graph maintenance & sub-goal scheduling                 │
│  • Per-session context handoffs (progress.md, goals.json)       │
│  • Mid-task milestone validation with pause/abort/retry         │
│  • Spin detection (loops, stagnation, oscillation)              │
│  • Auto-summarization at context-window boundaries              │
│  • Checkpoint + resume (crash, OOM, network blip)               │
│  • Trial setup with multiple strategies + early stopping        │
│  • HITL gates with Slack / web pause-resume                     │
│  • Real-time SSE/WebSocket trajectory stream                    │
│  • Durable state (SQLite default → PostgreSQL via Podman)       │
│  • CLI + Python SDK + optional web dashboard                    │
└─────────────────────────────────────────────────────────────────┘
```

---

## Core architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                       HorizonX Runtime                           │
│                                                                  │
│   ┌──────────┐   ┌──────────┐   ┌──────────────┐               │
│   │   Goal   │   │  Session │   │    Strategy  │               │
│   │  Graph   │←→ │ Manager  │ ←→│   Selector   │               │
│   │ (durable)│   │          │   │              │               │
│   └──────────┘   └──────────┘   └──────────────┘               │
│         ↓              ↓                ↓                        │
│   ┌──────────────────────────────────────────────┐              │
│   │           Agent Driver (pluggable)            │              │
│   │  Claude Code │ Codex │ OpenHands │ Custom    │              │
│   └──────────────────────────────────────────────┘              │
│         ↓                                                        │
│   ┌──────────┐   ┌──────────┐   ┌──────────────┐               │
│   │Trajectory│   │Milestone │   │     Spin     │               │
│   │ Recorder │   │Validators│   │   Detector   │               │
│   └──────────┘   └──────────┘   └──────────────┘               │
│         ↓              ↓                ↓                        │
│   ┌──────────────────────────────────────────────┐              │
│   │         Durable Store (SQLite / Postgres)     │              │
│   │   eval_runs · sessions · steps · gates · hitl │              │
│   └──────────────────────────────────────────────┘              │
│         ↓                                                        │
│   ┌──────────────────────────────────────────────┐              │
│   │       Event Bus → SSE / WebSocket / CLI       │              │
│   └──────────────────────────────────────────────┘              │
└──────────────────────────────────────────────────────────────────┘
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the deep dive on every box.

---

## The 8 execution strategies

Long-horizon tasks aren't all the same shape. HorizonX ships eight first-class execution strategies and lets you write your own. Pick by the task; mix and nest if you need.

| Strategy | Best for | Pattern |
|---|---|---|
| **Single-Session** | Small tasks (<30 steps) | One agent invocation, run to completion |
| **Sequential Sub-goals** *(Anthropic pattern)* | Feature builds, migrations, ML pipelines | Initializer → goal graph → one sub-goal per session |
| **Ralph Loop** *(Karpathy autoresearch)* | Optimization, hyperparameter search, content refinement | Time-boxed iterations, metric-driven retention |
| **Tree-of-Trials** | Hard / ambiguous problems | N parallel agents, different strategies, best wins |
| **Monitor-Respond** | SRE, ops, security | Long-lived agent watches signals, reacts on conditions |
| **Decomposition-First** | Very complex / high-stakes | Plan-only first session → HITL review → then execute |
| **Pair-Programming** | Quality-critical output | Builder agent + critic agent gating each step |
| **Self-Critique** | Code quality uplift | Agent iterates, LLM/shell critic gates each round |

See [docs/EXECUTION_STRATEGIES.md](docs/EXECUTION_STRATEGIES.md) for the full catalog with code examples.

---

## Quickstart

### Install

```bash
pip install horizonx              # core
pip install horizonx[postgres]    # for team / leaderboard mode
pip install horizonx[dashboard]   # for the web UI
```

### Run an example

```bash
# Folder-based (convention)
horizonx run examples/coding_oauth/ --agent claude-code --strategy sequential

# YAML config (full control)
horizonx run --config configs/my_task.yaml --resume run-abc123

# Watch live progress
horizonx watch run-abc123                      # terminal dashboard
horizonx serve --port 8080                     # web UI on :8080
```

### Define a task in YAML

```yaml
# examples/coding_oauth/task.yaml
id: build-oauth-001
name: Implement OAuth 2.0 Authorization Code Flow
horizon_class: very_long          # short / medium / long / very_long
estimated_duration_hours: [4, 12]

execution:
  strategy: sequential_sub_goals
  initializer:
    decompose_to_goal_graph: true
    target_subgoals: [40, 80]      # min, max sub-goals
  per_session:
    max_steps: 50
    max_minutes: 25
    one_subgoal_at_a_time: true     # Anthropic pattern

agent:
  type: claude_code
  model: claude-opus-4-7
  thinking_budget: 10000
  allowed_tools: [Read, Edit, Bash, Glob, Grep]
  per_session_session_id: true     # resume across crashes

context_management:
  handoff_files:
    - progress.md                   # human-readable narrative
    - goals.json                    # structured goal graph
    - decisions.jsonl               # decisions + rationale
    - failures.jsonl                # what was tried & failed
  summarizer:
    trigger_at_context_pct: 70
    model: claude-haiku-4-5         # cheap summarizer

milestone_validators:
  - id: tests_pass
    runs: after_every_session
    type: test_suite
    command: pytest tests/ -k oauth --tb=short
    on_fail: pause_for_hitl
  - id: build_works
    runs: after_every_session
    type: shell
    command: docker build .
    on_fail: retry_with_modification
  - id: progress_check
    runs: every_n_sessions
    n: 5
    type: llm_judge
    rubric_file: rubrics/progress.yaml
    on_fail: switch_strategy

spin_detection:
  exact_loop_threshold: 3
  stagnation_window_sessions: 3
  stagnation_score_delta: 0.02
  on_spin: terminate_and_alert

hitl:
  gate_on:
    - milestone_failure
    - spin_detected
    - resource_limit_50pct
  notification: slack
  channel: "#horizonx-alerts"

resources:
  max_total_hours: 12
  max_total_tokens: 5_000_000
  max_total_usd: 50
```

### Define a task in Python (full power)

```python
from horizonx import Task, Runtime, ClaudeCodeAgent, SequentialSubgoals
from horizonx.validators import TestSuiteGate, LLMProgressGate
from my_validators import SecurityScanGate

task = Task(
    id="build-oauth-001",
    prompt=open("prompts/oauth.md").read(),
    horizon_class="very_long",
    strategy=SequentialSubgoals(
        target_subgoals=(40, 80),
        per_session_max_steps=50,
        one_subgoal_at_a_time=True,
    ),
    agent=ClaudeCodeAgent(
        model="claude-opus-4-7",
        thinking_budget=10000,
        allowed_tools=["Read", "Edit", "Bash", "Glob", "Grep"],
    ),
    milestone_validators=[
        TestSuiteGate(runs="after_every_session", command="pytest tests/"),
        LLMProgressGate(runs_every_n=5, model="claude-haiku-4-5"),
        SecurityScanGate(runs="after_every_session"),
    ],
    handoff_files=["progress.md", "goals.json", "decisions.jsonl"],
    spin_detection={"exact_loop_threshold": 3, "stagnation_window": 3},
    resources={"max_hours": 12, "max_tokens": 5_000_000, "max_usd": 50},
)

runtime = Runtime(db_url="sqlite:///horizonx.db")
report = await runtime.run(task)
```

---

## Eight runnable examples — one per strategy

Each is a fully-spec'd `task.yaml` under `examples/`. Pick the closest one and adapt.

| Folder | Domain | Strategy | Duration | Validators |
|---|---|---|---|---|
| [`autoresearch/`](examples/autoresearch/) | ML research *(Karpathy wrap)* | Ralph loop | overnight | val_bpb metric |
| [`autotrain/`](examples/autotrain/) | ML training pipeline | Sequential sub-goals | 4–12h | data quality, AUC threshold, deploy smoke |
| [`kernel_optimization/`](examples/kernel_optimization/) | CUDA/Triton kernels | Ralph loop | 4–24h | correctness, throughput improvement |
| [`data_analysis/`](examples/data_analysis/) | Data science | Sequential sub-goals | 2–8h | notebook executes, checks pass, narrative |
| [`coding/`](examples/coding/) | Software (build OAuth) | Sequential sub-goals | 4–12h | tests, build, security scan |
| [`self_critique/`](examples/self_critique/) | Code quality uplift | Self-critique loop | 30–120min | mypy, pytest, radon cc |
| [`security_audit/`](examples/security_audit/) | Security hardening | Tree-of-trials | 2–6h | semgrep, bandit, OWASP scan |
| [`api_design/`](examples/api_design/) | API design + implementation | Decomposition-first | 4–8h | OpenAPI validates, tests pass |

See [`docs/LONG_HORIZON_AGENT.md`](docs/LONG_HORIZON_AGENT.md) Part VI for in-depth walkthroughs of each.

---

## How HorizonX prevents long-horizon failure modes

| Failure mode | HorizonX mitigation |
|---|---|
| **Agent loses the plot mid-task** | Goal graph (`goals.json`) loaded at every session start; auto-injected into prompt |
| **Context window exhaustion** | Per-session scope cap + auto-summarizer at 70% utilization → fresh session reads handoff files |
| **Agent declares premature victory** | Granular sub-goals (Anthropic pattern: 200+ feature list); `passes` field is the only thing the agent can flip; milestone validators verify before accepting |
| **Cyclic loops / oscillation** | Multi-layer spin detector: exact tool repetition, edit-revert, score plateau, semantic LLM-judge "is this making progress" |
| **Crash mid-task = lost work** | Every step persisted; session_id saved for Codex/Claude Code resume; restart from last commit + last handoff |
| **Silent stagnation** | Mandatory milestone validators run between sessions; pause-for-HITL on N consecutive failures |
| **Cost runaway** | Hard token / wall-clock / dollar budgets enforced at runtime; alert at 50% / 75% |
| **Operator blindness** | SSE event stream → terminal `watch` UI / web dashboard / Slack notifications |
| **Wrong strategy for the task** | Strategy-specific runtime: Ralph loop ≠ Sequential sub-goals ≠ Monitor-respond — pick by task shape |

See [docs/ANTI_CYCLING.md](docs/ANTI_CYCLING.md) for the spin-detection deep dive.

---

## Hyperoptimized for Claude Code & Codex

Both Claude Code CLI and Codex CLI expose **streaming JSONL events** and **session resume**. HorizonX is built around this:

**Claude Code driver leverages:**
- `--output-format stream-json` → real-time trajectory ingest
- `--thinking <budget>` for hard reasoning steps
- `--allowedTools` per task type (coding gets Edit+Bash; research gets WebSearch only)
- Session resume via saved session_id
- Per-task MCP server injection
- `--bare` for clean programmatic mode

**Codex CLI driver leverages:**
- `codex exec --json` JSONL streaming
- `codex exec resume <session_id>` for crash recovery
- `--reasoning-effort high/medium/low` per task economic profile
- Prompt via stdin (avoids argv-length limits — pattern from Atlas's CodexBridge)
- Per-step wall-clock timeout enforcement

**Other agents** (`OpenHands`, your own) implement `BaseAgent.run(task, env) -> AsyncIterator[TrajectoryStep]` and inherit all observability and recovery.

See [docs/AGENT_DRIVERS.md](docs/AGENT_DRIVERS.md) for driver internals.

---

## Stack & dependencies

- **Python 3.11+** — `asyncio` everywhere
- **SQLAlchemy 2.0 async** — durable store
- **SQLite** (default, zero setup) **/ PostgreSQL** (via Podman, multi-tenant) — your choice
- **Pydantic v2** — typed task / config / event schemas
- **Click** — CLI
- **FastAPI + SSE** — optional web dashboard
- **Rich** — terminal UI
- **No LangGraph dependency** — HorizonX is a *test runner / workflow engine*, not an agent. You can still invoke LangGraph-based agents through the `BaseAgent` interface.

Containers via **Podman** (rootless) by default; Docker also supported.

---

## Where to start

The complete design — concepts, architecture, strategies, context management, anti-cycling, operations, use cases, and implementation roadmap — is in **one document**:

📖 **[`docs/LONG_HORIZON_AGENT.md`](docs/LONG_HORIZON_AGENT.md)**

Skim Part I (Foundations) for the conceptual grounding, Part II–V for the architecture, Part VI for use cases, Part VII for the implementation roadmap.

## Project status

**Alpha — core runtime is implemented and tested.**

| Component | Status |
|---|---|
| Core runtime (run, session, goal graph, event bus) | ✅ Implemented |
| All 8 execution strategies | ✅ Implemented |
| All 6 milestone validators | ✅ Implemented |
| Claude Code + Codex + OpenHands + Custom agent drivers | ✅ Implemented |
| Spin detection (4-layer: exact, edit-revert, plateau, semantic) | ✅ Implemented |
| Summarizer with Anthropic SDK + prompt caching | ✅ Implemented |
| Fork/merge runs | ✅ Implemented |
| SQLite durable store | ✅ Implemented |
| CLI (run, watch, show, list, export, serve) | ✅ Implemented |
| Test suite | ✅ 206 tests, all passing |
| Web dashboard (FastAPI + SSE) | 🔧 In progress |
| PostgreSQL backend | 🔧 In progress |
| aiosqlite async storage | 🔧 Planned |

Contributions welcome — please open an issue or PR.

---

## License

Apache 2.0 — see [LICENSE](LICENSE).

---

## Acknowledgements & references

- [Anthropic — Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) — the two-agent + feature-list pattern
- [Karpathy — autoresearch](https://github.com/karpathy/autoresearch) — Ralph loop for ML research
- [SWE-bench](https://www.swebench.com/) — gold-standard eval harness; informs our trajectory schema
- [Inspect AI (UK AISI)](https://inspect.aisi.org.uk/) — sandboxing primitives
- [τ-Bench](https://github.com/sierra-research/tau-bench) — pass^k as reliability metric
- [Temporal.io](https://temporal.io/) — workflow durability concepts; HorizonX is the agent-shaped analog
