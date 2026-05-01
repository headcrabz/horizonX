"""HorizonX — Python API example.

Equivalent to examples/coding/task.yaml but defined entirely in Python.
Run from the repo root:

    python examples/python_api.py

The run writes progress to ./horizonx-workspaces/<run-id>/ and persists
state to ./horizonx.db (created automatically).
"""

from __future__ import annotations

import asyncio

from horizonx import Runtime, Task
from horizonx.storage import SqliteStore


task = Task(
    id="build-oauth-001",
    name="Implement OAuth 2.0 Authorization Code Flow with PKCE",
    description=(
        "Add OAuth 2.0 (authorization code + PKCE + refresh + revocation) "
        "to an existing FastAPI app, with full test coverage and a security scan gate."
    ),
    horizon_class="very_long",
    estimated_duration_hours=(4.0, 12.0),
    tags=["coding", "security", "fastapi"],
    prompt="""\
Implement OAuth 2.0 Authorization Code Flow with PKCE in this FastAPI app.

REQUIRED:
  1. /authorize endpoint with PKCE (code_challenge S256)
  2. /token endpoint (code → token exchange)
  3. /refresh and /revoke endpoints
  4. Client registration table + admin endpoints
  5. Scope validation middleware on protected routes
  6. Full test coverage for each endpoint + an end-to-end integration test
  7. OpenAPI docs reflect new endpoints
  8. bandit security scan reports 0 critical issues

CONSTRAINTS:
  - Do not delete or weaken existing tests.
  - All persistence through the existing SQLAlchemy session pattern.
  - Tokens never logged; secrets only in env vars; cookies HttpOnly + Secure.
""",
    strategy={
        "kind": "sequential",
        "config": {
            "target_subgoals": [40, 80],
            "max_attempts_per_goal": 3,
            "git_commit_each_session": True,
        },
    },
    agent={
        "type": "claude_code",
        "model": "claude-opus-4-7",
        "thinking_budget": 10000,
        "allowed_tools": ["Read", "Edit", "Write", "Bash", "Glob", "Grep"],
    },
    environment={
        "type": "local",
        "setup_commands": [
            "python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt"
        ],
    },
    milestone_validators=[
        {
            "id": "tests_pass",
            "type": "test_suite",
            "runs": "after_every_session",
            "on_fail": "pause_for_hitl",
            "config": {
                "command": "pytest tests/ -k oauth --tb=short -q",
                "test_dir": "tests/",
                "min_test_count": 1,
                "timeout_seconds": 120,
            },
        },
        {
            "id": "server_starts",
            "type": "shell",
            "runs": "after_every_session",
            "on_fail": "pause_for_hitl",
            "config": {
                "command": "timeout 5 uvicorn app.main:app --host 127.0.0.1 --port 0 || true",
                "timeout_seconds": 10,
            },
        },
        {
            "id": "security_scan",
            "type": "shell",
            "runs": "every_n_sessions",
            "n": 5,
            "on_fail": "pause_for_hitl",
            "config": {"command": "bandit -r app/ --severity-level high -q"},
        },
    ],
    handoff_files=[
        "progress.md",
        "goals.json",
        "decisions.jsonl",
        "failures.jsonl",
        "summary.md",
    ],
    summarizer={"enabled": True, "trigger_at_context_pct": 70},
    spin_detection={
        "enabled": True,
        "exact_loop_threshold": 3,
        "edit_revert_enabled": True,
        "on_spin": "terminate_and_hitl",
    },
    hitl={
        "enabled": True,
        "triggers": ["validator_paused", "spin_detected", "subgoal_max_attempts"],
        "notification_type": "console",
    },
    resources={
        "max_total_hours": 12.0,
        "max_total_usd": 60.0,
        "max_total_tokens": 6_000_000,
        "max_sessions": 100,
        "max_steps_per_session": 50,
        "max_minutes_per_session": 25.0,
    },
)


async def main() -> None:
    store = SqliteStore("horizonx.db")
    runtime = Runtime(store=store)
    run = await runtime.run(task)
    print(f"Run {run.id} finished with status: {run.status}")


if __name__ == "__main__":
    asyncio.run(main())
