"""Environment, redaction, permission, and trust-boundary contracts."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from horizonx.agents.base import Workspace, stream_subprocess_jsonl
from horizonx.agents.claude_code import ClaudeCodeAgent
from horizonx.agents.custom import CustomAgent
from horizonx.core.runtime import Runtime
from horizonx.core.types import (
    AgentConfig,
    EnvironmentConfig,
    Step,
    StrategyConfig,
    Task,
)
from horizonx.environments.base import SetupCommandError
from horizonx.environments.git import GitWorktreeBackend
from horizonx.storage.sqlite import SqliteStore


@pytest.mark.asyncio
async def test_unrelated_parent_secret_is_not_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HORIZONX_UNRELATED_SECRET", "must-not-cross")
    script = (
        "import json,os; "
        "print(json.dumps({'visible': os.getenv('HORIZONX_UNRELATED_SECRET')}))"
    )

    events = [
        event
        async for event in stream_subprocess_jsonl(
            [sys.executable, "-c", script], cwd=tmp_path, env={}
        )
    ]

    assert events == [{"visible": None}]


@pytest.mark.asyncio
async def test_injected_credentials_are_redacted_before_events(
    tmp_path: Path,
) -> None:
    secret = "local-test-secret-value"
    script = (
        "import json,os; "
        "print(json.dumps({'message': 'token=' + os.environ['OPENAI_API_KEY']}))"
    )

    events = [
        event
        async for event in stream_subprocess_jsonl(
            [sys.executable, "-c", script],
            cwd=tmp_path,
            env={"OPENAI_API_KEY": secret},
        )
    ]

    assert events == [{"message": "token=<redacted>"}]


@pytest.mark.asyncio
async def test_custom_agent_does_not_inherit_parent_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UNRELATED_PASSWORD", "parent-only")
    script = (
        "import json,os; "
        "print(json.dumps({'content': {'visible': os.getenv('UNRELATED_PASSWORD')}}))"
    )
    steps: list[Step] = []
    agent = CustomAgent(
        AgentConfig(
            type="custom",
            model="test",
            extra={
                "command": [sys.executable, "-c", script],
                "output_format": "jsonl",
            },
        )
    )

    async def collect(step: Step) -> None:
        steps.append(step)

    await agent.run_session(
        "inspect",
        Workspace(tmp_path, {}),
        on_step=collect,
    )

    assert steps[0].content == {"visible": None}


def test_claude_uses_safe_permissions_by_default() -> None:
    agent = ClaudeCodeAgent(
        AgentConfig(type="claude_code", model="claude-test")
    )

    command = agent._build_command(None, "session-id")
    permission_index = command.index("--permission-mode")
    assert command[permission_index + 1] != "bypassPermissions"


def test_claude_bypass_requires_explicit_acknowledgement() -> None:
    with pytest.raises(ValueError, match="allow_unsafe_permissions"):
        ClaudeCodeAgent(
            AgentConfig(
                type="claude_code",
                model="claude-test",
                extra={"permission_mode": "bypassPermissions"},
            )
        )


def test_claude_bypass_accepts_explicit_acknowledgement() -> None:
    agent = ClaudeCodeAgent(
        AgentConfig(
            type="claude_code",
            model="claude-test",
            extra={
                "permission_mode": "bypassPermissions",
                "allow_unsafe_permissions": True,
            },
        )
    )

    command = agent._build_command(None, "session-id")
    permission_index = command.index("--permission-mode")
    assert command[permission_index + 1] == "bypassPermissions"


@pytest.mark.asyncio
async def test_attempt_snapshot_records_active_trust_boundary(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store, workspace_root=tmp_path / "workspaces")
    task = Task(
        id="trust-boundary",
        name="Trust boundary",
        prompt="Record the boundary",
        strategy=StrategyConfig(kind="single"),
        agent=AgentConfig(type="mock", model="mock"),
    )
    try:
        run = await runtime.run(task)
        attempt = (await store.list_attempts(run.id))[0]
        boundary = attempt.workspace_snapshot["trust_boundary"]

        assert boundary["environment"] == "allowlist"
        assert boundary["process"] == "new_session"
        assert boundary["network"] == "host_unrestricted"
        assert boundary["workspace"] == "read_write"
        assert "environment_keys" in boundary
        assert not any("secret" in value.lower() for value in boundary.values() if isinstance(value, str))
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_agent_output_is_redacted_before_durable_recording(tmp_path: Path) -> None:
    secret = "durable-secret-value"
    script = (
        "import json,os; print(json.dumps({"
        "'content': {'text': 'credential=' + os.environ['SERVICE_TOKEN']}}))"
    )

    store = SqliteStore(tmp_path / "redaction.db")
    runtime = Runtime(store, workspace_root=tmp_path / "redaction-workspaces")
    task = Task(
        id="durable-redaction",
        name="Durable redaction",
        prompt="Do not persist credentials",
        strategy=StrategyConfig(kind="single"),
        agent=AgentConfig(
            type="custom",
            model="custom",
            extra={
                "command": [sys.executable, "-c", script],
                "output_format": "jsonl",
            },
        ),
        environment=EnvironmentConfig(
            inherit_env=[], env={"SERVICE_TOKEN": secret}
        ),
    )
    try:
        run = await runtime.run(task)
        session = (await store.list_sessions(run.id))[0]
        steps = await store.recent_steps(session.id, 10)

        assert steps[0].content == {"text": "credential=<redacted>"}
        assert secret not in (run.workspace_path / "trajectory.jsonl").read_text()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_setup_failure_redacts_explicit_secret(tmp_path: Path) -> None:
    secret = "setup-secret-value"
    command = (
        f"{sys.executable} -c \"import os,sys; "
        "sys.stderr.write(os.environ['BUILD_TOKEN']); sys.exit(9)\""
    )
    backend = GitWorktreeBackend(
        tmp_path / "setup-workspaces",
        EnvironmentConfig(
            setup_commands=[command],
            inherit_env=[],
            env={"BUILD_TOKEN": secret},
        ),
    )

    with pytest.raises(SetupCommandError) as captured:
        await backend.prepare("run-redacted-setup", None)

    assert secret not in str(captured.value)
    assert captured.value.result.stderr == "<redacted>"
