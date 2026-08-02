# HorizonX

<p align="center">
  <img src="docs/horizonx_banner.png" alt="HorizonX — Long-horizon agent execution harness" width="100%"/>
</p>

**Alpha research preview of a vendor-neutral control plane for long-horizon agent tasks.**

HorizonX coordinates Claude Code, Codex, OpenHands, and custom agents around durable run state,
goal graphs, validation, resource policies, and operator controls. The repository contains tested
building blocks for those capabilities, but several end-to-end guarantees are still being
hardened. It is suitable for local evaluation and development—not unattended or production use.

[![CI](https://github.com/headcrabz/horizonX/actions/workflows/ci.yml/badge.svg)](https://github.com/headcrabz/horizonX/actions)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![Tests: 365 passing](https://img.shields.io/badge/tests-365%20passing-brightgreen.svg)](tests/)

---

## What it does

HorizonX is not an agent. It is an experimental runtime layer around existing agent harnesses.

The current alpha includes:

- **Durable records** — SQLite stores runs, sessions, steps, validations, HITL records, and usage.
  Exact crash-point recovery and authoritative goal persistence are under hardening.
- **Spin-analysis components** — seven detectors cover repetition, oscillation, plateaus, and
  cross-session stagnation. Universal invocation and response semantics are not yet guaranteed.
- **Resource-policy components** — token, cost, time, and workspace-budget models exist. Uniform
  enforcement across all strategies and providers is under hardening.
- **Knowledge components** — FTS5 knowledge storage and Markdown handoff sync exist as modules.
  Automatic cross-run sync and prompt injection are not yet wired into the production run path.
- **Goal graphs and validators** — DAG planning and six validator types are implemented. Database
  authority, terminal-state correctness, and evidence-backed completion are under hardening.
- **Eight strategy modules** — single, sequential, pair, tree, self-critique, decomposition,
  monitor, and ralph. They are experimental until they share one verified execution lifecycle.
- **Operator and observability components** — Slack HITL, dashboard SSE, and CLI watch exist.
  Durable commands, real cancellation, authenticated callbacks, and event replay are planned.

See the [support matrix](#project-status) before relying on a capability.

---

## Quick start

```bash
pip install -e ".[dev]"
export ANTHROPIC_API_KEY=sk-ant-...

# Run a task
horizonx run examples/demo_word_counter/task.yaml

# Watch it live
horizonx watch <run-id>

# Resume an existing run at a supported session boundary (experimental)
horizonx run task.yaml --resume <run-id>

# Launch the dashboard
pip install -e ".[dashboard]"
horizonx serve
```

The checked-in Compose file is currently a development stub, not a supported quick start. Image
packaging, container binding, and health checks remain under hardening.

---

## Task definition

```yaml
id: refactor-auth-001
name: Refactor authentication to JWT
prompt: |
  Replace session-cookie auth with JWT tokens.
  Use RS256 signing. Update all protected routes.
  All existing tests must continue to pass.

strategy:
  kind: sequential
  config:
    max_attempts_per_goal: 3
    git_commit_each_session: true

agent:
  type: claude_code
  model: claude-opus-4-8

milestone_validators:
  - id: tests_pass
    type: test_suite
    runs: after_every_session
    on_fail: pause_for_hitl
    config:
      command: pytest tests/ -q

hitl:
  notification_type: slack
  notification_target: "#eng-alerts"
  timeout_minutes: 30
  escalation_action: approve

resources:
  max_total_usd: 5.0
  max_total_tokens: 500000
```

---

## Execution strategies

| Strategy | Use when |
|---|---|
| `single` | Quick tasks under ~30 steps |
| `sequential` | Feature builds, migrations — one sub-goal per session with filesystem handoffs |
| `pair` | Quality-critical output — driver codes, navigator reviews each step |
| `self_critique` | Code quality — agent critiques its own output before marking done |
| `decomposition` | Complex tasks — LLM decomposes into sub-tasks first |
| `tree` | Ambiguous problems — parallel branches, best result wins |
| `monitor` | Long-lived watching — fires when conditions trigger |
| `ralph` | Iterative optimization — time-boxed loops with metric-driven retention |

---

## Spin detection layers

Loops that the agent can't detect itself are caught by HorizonX automatically:

| Layer | Catches |
|---|---|
| `ExactLoop` | Identical tool calls repeated ≥ N times |
| `BucketedHash` | Semantically similar calls with minor argument variation |
| `EditRevert` | A→B→A→B file flip-flop oscillation |
| `ToolThrashing` | Same tool dominating with no new outputs (idempotent reads) or same mutating args |
| `ScorePlateau` | Validator scores flat across N sessions — no real progress |
| `SemanticProgress` | LLM judge: is the agent actually advancing on the stated goal? |
| `CrossSession` | Multiple sessions completed but zero goal nodes transitioned to DONE |

Soft-threshold triggers a diagnostic injection. Hard-threshold terminates the session and notifies the operator.

---

## Budget governance

```yaml
resources:
  max_total_usd: 10.0          # hard stop
  max_total_tokens: 1_000_000

workspace:
  workspace_id: my-project
  daily_budget_usd: 25.0       # across all runs today
```

- Charges are tracked from real token counts in agent stream events
- Slack alert fires at 75% via `asyncio.create_task` (non-blocking)
- Cost velocity detector fires when $/min rate doubles twice in succession
- Pre-flight check blocks new runs when workspace daily budget is exhausted

---

## Cross-session memory

Agents write facts as Markdown during sessions:

```markdown
# workspace/knowledge/auth.md
---
tags: [jwt, security, python]
---
PyJWT 2.8+ requires explicit algorithm parameter in decode().
Use: jwt.decode(token, key, algorithms=["HS256"])
```

After each session, HorizonX:
1. Indexes all `knowledge/*.md` files into a per-workspace FTS5 SQLite store
2. On the next run, searches relevant facts by goal name + description
3. Injects pinned facts unconditionally + top-K relevant facts into the session prompt

Facts persist across runs indefinitely. Agents stop re-discovering the same things.

---

## Agent harness setup

HorizonX ships drivers for four agents. Each needs its own binary or API key:

### Claude Code (default)
```bash
# Install Claude Code CLI
npm install -g @anthropic-ai/claude-code

# Authenticate (subscription or API key)
claude /login
# OR
export ANTHROPIC_API_KEY=sk-ant-...
```
```yaml
agent:
  type: claude_code
  model: claude-opus-4-8        # or claude-sonnet-4-6, claude-haiku-4-5
  extra:
    permission_mode: bypassPermissions   # acceptEdits | auto | bypassPermissions
    max_budget_usd: 2.0                  # native claude budget cap (optional)
```

### Codex (OpenAI)
```bash
npm install -g @openai/codex
export OPENAI_API_KEY=sk-...
```
```yaml
agent:
  type: codex
  model: codex-mini-latest      # or o4-mini
```

### OpenHands
```bash
pip install openhands-ai
export ANTHROPIC_API_KEY=sk-ant-...   # or OPENAI_API_KEY
```
```yaml
agent:
  type: openhands
  model: claude-opus-4-8
```

### Custom / SDK agent (no binary needed)
```python
from pathlib import Path
from horizonx.core.types import Step, StepType

async def my_agent(prompt: str, workspace_path: Path):
    # yield Step objects as your agent works
    yield Step(session_id="", sequence=0, type=StepType.THOUGHT,
               content={"text": "Starting task..."})
    # ... do work, yield more steps ...

# Register inline:
task = Task(agent=AgentConfig(type="sdk", extra={"callable": my_agent}))
```

Or ship as a pip package with an entry-point (see [CONTRIBUTING.md](CONTRIBUTING.md)).

---

## Pluggable architecture

All extension points use Python entry-points — ship a pip package and it works:

```toml
# Your plugin's pyproject.toml
[project.entry-points."horizonx.agents"]
my_agent = "my_package.agents:MyAgent"

[project.entry-points."horizonx.strategies"]
my_strategy = "my_package.strategies:MyStrategy"

[project.entry-points."horizonx.validators"]
my_gate = "my_package.validators:MyGate"
```

For API-based agents (no subprocess needed):

```python
async def my_agent_fn(prompt: str, workspace_path: Path):
    # yield Step objects as your agent works
    yield Step(session_id="", sequence=0, type=StepType.THOUGHT, content={"text": "..."})

task = Task(agent=AgentConfig(type="sdk", extra={"callable": my_agent_fn}))
```

---

## Target architecture

This diagram is the intended converged lifecycle. In the alpha, strategy modules still invoke
parts of the lifecycle differently; the [support matrix](#project-status) is authoritative.

```
Task (YAML / Python)
  → Runtime.run()
    → Budget pre-check (workspace daily limit)
    → Strategy.execute()         (decides session loop shape)
      → SessionManager           (composes prompt + knowledge injection)
        → Agent.run_session()    (spawns subprocess, yields Steps)
          → TrajectoryRecorder   (persists Steps to SQLite + JSONL)
        → SpinDetector           (7 layers, in-session + cross-session)
        → Runtime.run_validators (gates: continue / pause / abort)
        → ResourceGovernor       (charges tokens/cost, checks thresholds)
        → KnowledgeHandoffDir    (indexes knowledge/*.md to FTS5)
    → EventBus                   (SSE → dashboard / CLI watch / Slack)
    → SqliteStore                (local durable state; recovery hardening in progress)
```

---

## Development

```bash
# Install with all extras
pip install -e ".[dev,dashboard,slack]"

# Run tests
pytest

# Lint
ruff check horizonx/ tests/

# Type check
mypy horizonx/ --ignore-missing-imports
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to write custom agents, strategies, and validators.

---

## Project status

HorizonX is an **alpha research preview**. “Implemented” below means the component exists and has
focused tests; it does not imply a verified production guarantee.

| Capability | Alpha status | Current boundary |
|---|---|---|
| Python models, registries, and plugin entry points | Implemented | Third-party agent and validator names are supported; third-party strategy names are blocked by the current config schema. |
| CLI execution, inspection, dashboard, and database maintenance | Implemented subset | `run`, `show`, `list`, `watch`, `fork`, `export`, `serve`, `doctor`, `backup`, `restore`, and `checkpoint` are available; broader operator workflows are planned. |
| SQLite orchestration persistence | Implemented, local-only | Run-scoped goal identity, migrations, foreign keys, bounded contention, integrity checks, backup, restore, and atomic goal transitions are implemented. Use one local HorizonX daemon and keep the database on a local filesystem. |
| Claude Code, Codex, OpenHands, custom, and SDK drivers | Experimental | CLI transports exist; structured native transports, capability negotiation, and cross-provider parity are not verified. |
| Eight execution strategy modules | Experimental | Lifecycle, failure, validator, budget, cleanup, and recovery behavior is not yet uniform. |
| Goal graph and six validator types | Experimental | SQLite is authoritative and `goals.json` is an atomic human-readable projection; terminal-state and evidence semantics still need hardening. |
| Resume and dashboard pending-run recovery | Under hardening | Resume is session-boundary oriented; a crash can lose provider resume state or strand a pending run. Exact crash-point recovery is not claimed. |
| Seven spin-detection components | Under hardening | The sequential path invokes the combined detector; universal wiring and configured response behavior are not yet verified. |
| Resource governor and usage store | Under hardening | Enforcement and accounting are not uniform across strategies/providers; unknown provider cost must not be interpreted as zero. |
| FTS5 knowledge store and handoff sync | Components only | The store and sync modules are not yet connected to the normal runtime/strategy lifecycle. Automatic cross-run memory is not claimed. |
| Slack HITL and dashboard controls | Under hardening | Durable decisions, authenticated callbacks, restart-safe waits, and process-tree cancellation are not complete. |
| SSE dashboard and JSONL trajectory | Experimental | SSE is in-memory and has no durable cursor/replay guarantee. |
| Local workspace execution | Experimental | Runs currently materialize an empty local workspace; repository checkout and setup-command semantics need hardening. |
| Docker, Podman, and E2B execution | Configuration only | Configuration models exist, but non-local environment execution is not wired into the runtime. |
| Multi-run/multi-worker concurrency | Not supported | The SQLite backend is intentionally single-daemon; durable leases, worker coordination, and a server database are required for distributed execution. |
| Automated verification | Implemented | 365 tests pass on the audited baseline; Ruff and Mypy pass. This is component coverage, not a production certification. |
| Docker distribution | Not yet supported | The Compose file is a development stub; a real image, binding, health, install, and smoke-test path are planned. |

Until the “under hardening” rows have end-to-end recovery and invariant evidence, do not market or
operate HorizonX as a production-grade long-horizon controller.

---

## License

Apache 2.0 — see [LICENSE](LICENSE).

Built by [Anshul Mittal](https://www.linkedin.com/in/anshulnsit/).
