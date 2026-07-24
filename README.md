# HorizonX

<p align="center">
  <img src="docs/horizonx_banner.png" alt="HorizonX — Long-horizon agent execution harness" width="100%"/>
</p>

**A production-grade execution harness for long-horizon AI agent tasks.**

HorizonX wraps Claude Code, Codex, and OpenHands with the infrastructure they need to run reliably for hours — crash recovery, spin detection, budget governance, cross-session memory, structured goal tracking, and operator-in-the-loop gates.

[![CI](https://github.com/your-org/horizonx/actions/workflows/ci.yml/badge.svg)](https://github.com/your-org/horizonx/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![Tests: 277 passing](https://img.shields.io/badge/tests-277%20passing-brightgreen.svg)](tests/)

---

## What it does

HorizonX is not an agent. It's the runtime layer that makes agents reliable.

You bring a task. HorizonX handles the rest:

- **Crash recovery** — every tool call is persisted before the next one runs. Kill the process at any moment; resume from exactly where it stopped using the agent's own session ID.
- **7-layer spin detection** — catches loops, oscillation, score plateaus, tool thrashing, and cross-session stagnation before they cost real money.
- **Budget governance** — Slack alert at 75% spend, hard stop at 100%, per-workspace daily limits, cost velocity runaway detection.
- **Cross-session memory** — agents write facts to `workspace/knowledge/*.md`; HorizonX indexes them via FTS5 and injects relevant facts into future session prompts automatically.
- **Structured goal graph** — tasks decompose into a DAG; agents *propose* completion; validators *accept*. Premature completion is prevented by construction.
- **Pluggable strategies** — 8 built-in execution topologies (single, sequential, pair, tree, self-critique, decomposition, monitor, ralph). Switch strategies per task in YAML.
- **Operator gates** — Slack Block Kit HITL with approve / modify / re-decompose actions. Operators can restructure the goal graph mid-run.
- **Real-time observability** — SSE event stream to dashboard, CLI `watch`, and Slack.

---

## Quick start

```bash
pip install -e ".[dev]"
export ANTHROPIC_API_KEY=sk-ant-...

# Run a task
horizonx run examples/demo_governance/task.yaml

# Watch it live
horizonx watch <run-id>

# Resume after a crash
horizonx run task.yaml --resume <run-id>

# Launch the dashboard
pip install -e ".[dashboard]"
horizonx serve
```

Or with Docker:
```bash
docker compose up
# Dashboard at http://localhost:8080
```

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

## Architecture

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
    → SqliteStore                (all state persisted — WAL mode, crash-safe)
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

Core runtime is implemented, tested, and ready for production use on long-horizon tasks.

| Component | Status |
|---|---|
| Core runtime (run, session, goal graph, event bus) | ✅ |
| SQLite store — WAL mode, async ThreadPoolExecutor | ✅ |
| All 8 execution strategies | ✅ |
| 7-layer spin detector + cross-session layer | ✅ |
| Claude Code + Codex + OpenHands + SDK agent drivers | ✅ |
| Housekeeping step budget refund | ✅ |
| Real Slack HITL — Block Kit, retry, timeout escalation | ✅ |
| Budget governance — charge wiring, velocity runaway | ✅ |
| Cross-run knowledge store (FTS5) | ✅ |
| re_decompose HITL — LLM goal restructuring | ✅ |
| Dashboard — crash-safe launch, pending run recovery | ✅ |
| All milestone validators (shell, test_suite, llm_judge, metric, git, goal_graph) | ✅ |
| CI (GitHub Actions, Python 3.11 + 3.12) | ✅ |
| 277 tests, all passing | ✅ |
| PolicyEngine (T1 Python callables) | 🔧 Planned v1.1 |
| A2A Protocol interop | 🔧 Planned v1.1 |

---

## License

MIT — see [LICENSE](LICENSE).

Built by [Anshul Mittal](https://www.linkedin.com/in/anshulnsit/).
