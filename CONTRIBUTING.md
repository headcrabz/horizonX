# Contributing to HorizonX

## Setup

```bash
git clone https://github.com/your-org/horizonx
cd horizonx
pip install -e ".[dev,dashboard,slack]"
pytest                    # all tests green
ruff check horizonx/ tests/
```

## Writing a Custom Strategy

Implement the `Strategy` protocol and register via entry-points:

```python
# my_package/strategies.py
from collections.abc import AsyncIterator
from horizonx.core.event_bus import Event
from horizonx.core.types import Run

class MyStrategy:
    kind = "my_strategy"

    def __init__(self, config: dict):
        self.config = config

    async def execute(self, run: Run, rt) -> AsyncIterator[Event]:
        # rt exposes: start_session, end_session, record_step, check_spin,
        #             run_validators, request_hitl, charge, store, bus
        session = await rt.start_session(run)
        # ... do work ...
        await rt.end_session(session, SessionStatus.COMPLETED)
        yield Event(type="run.completed", run_id=run.id)
```

```toml
# pyproject.toml
[project.entry-points."horizonx.strategies"]
my_strategy = "my_package.strategies:MyStrategy"
```

## Writing a Custom Agent Driver

Copy `horizonx/agents/template.py`, implement `run_session()`, register:

```toml
[project.entry-points."horizonx.agents"]
my_agent = "my_package.agents:MyAgent"
```

Use in a task YAML:
```yaml
agent:
  type: my_agent
```

For API-based agents (no subprocess), use the built-in `sdk` type:
```python
task = Task(agent=AgentConfig(type="sdk", extra={"callable": my_async_gen}))
```

## Writing a Custom Validator

```python
from horizonx.core.types import GateDecision, GateAction

class MyGate:
    async def validate(self, run, session, workspace) -> GateDecision:
        # inspect workspace files, run tests, call APIs...
        return GateDecision(action=GateAction.CONTINUE, reason="looks good")
```

```toml
[project.entry-points."horizonx.validators"]
my_gate = "my_package.validators:MyGate"
```

## PR Guidelines

- `pytest tests/ -q` must pass before submitting
- `ruff check horizonx/ tests/` must pass (zero warnings)
- No new `print()` statements — use `logging` or `rt.bus.publish(Event(...))`
- New features need tests; bug fixes should include a regression test
- Keep PRs focused: one feature or fix per PR
- Add an entry to `CHANGELOG.md` under `[Unreleased]`

## Running the Dashboard Locally

```bash
pip install -e ".[dev,dashboard]"
horizonx serve
# open http://localhost:8080
```

## Adding Knowledge to an Agent Run

Agents can write `workspace/knowledge/<slug>.md` files during sessions.
HorizonX automatically indexes these into the cross-run knowledge store
and injects relevant facts into future session prompts.

Frontmatter is supported:
```markdown
---
tags: [auth, jwt, python]
---
PyJWT 2.8+ requires explicit algorithm specification in decode() calls.
Use `jwt.decode(token, key, algorithms=["HS256"])`.
```
