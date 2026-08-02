"""Integration coverage for built-in and third-party strategy execution paths."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from horizonx import AttemptExecutor
from horizonx.core.event_bus import Event
from horizonx.core.runtime import Runtime
from horizonx.core.types import (
    AgentConfig,
    Run,
    RunStatus,
    StrategyConfig,
    StrategyOutcome,
    Task,
)
from horizonx.storage.sqlite import SqliteStore

BUILTIN_STRATEGY_MODULES = (
    "decomposition",
    "monitor",
    "pair",
    "ralph",
    "self_critique",
    "sequential",
    "single",
    "tree",
)

BUILTIN_STRATEGY_CASES = (
    ("single", {}),
    ("sequential", {"git_commit_each_session": False}),
    ("decomposition", {}),
    ("pair", {"max_rounds": 1, "accept_threshold": 0.5}),
    ("tree", {"width": 1, "max_depth": 1, "accept_threshold": 0.5}),
    (
        "self_critique",
        {"max_rounds": 1, "critic_type": "shell", "critic_command": "true"},
    ),
    (
        "monitor",
        {"trigger_command": "true", "poll_interval_seconds": 0, "max_triggers": 1},
    ),
    (
        "ralph",
        {
            "total_minutes": 0.01,
            "fixed_minutes_per_iter": 0.01,
            "metric": {"measurement": "echo 1", "direction": "minimize"},
            "early_stopping": {"window": 1, "delta": 0.001},
        },
    ),
)


@pytest.mark.parametrize("module_name", BUILTIN_STRATEGY_MODULES)
def test_builtin_strategy_routes_agent_work_through_attempt_executor(
    module_name: str,
) -> None:
    source_path = Path(__file__).parents[1] / "horizonx" / "strategies" / f"{module_name}.py"
    source = source_path.read_text()

    assert "AttemptExecutor" in source
    assert ".run_session(" not in source
    assert ".start_session(" not in source
    assert ".end_session(" not in source
    assert "Workspace(" not in source


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "config"), BUILTIN_STRATEGY_CASES)
async def test_builtin_strategy_uses_persisted_attempt_lifecycle(
    tmp_path: Path, kind: str, config: dict[str, object]
) -> None:
    store = SqliteStore(tmp_path / f"{kind}.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / f"{kind}-workspaces")
    task = Task(
        id=f"{kind}-lifecycle",
        name=f"{kind} lifecycle",
        prompt="Exercise the built-in lifecycle",
        strategy=StrategyConfig(kind=kind, config=config),
        agent=AgentConfig(type="mock", model="mock"),
    )
    try:
        run = await runtime.run(task)

        sessions = await store.list_sessions(run.id)
        assert run.status == RunStatus.COMPLETED
        assert sessions
        assert all(session.status.value == "completed" for session in sessions)
        for session in sessions:
            assert len(await store.recent_steps(session.id, 20)) == 3
    finally:
        await store.close()


def test_strategy_config_accepts_plugin_names_and_rejects_blank_names() -> None:
    assert StrategyConfig(kind="third_party").kind == "third_party"
    with pytest.raises(ValidationError, match="strategy kind must not be blank"):
        StrategyConfig(kind="  ")


class _ThirdPartyStrategy:
    def __init__(self, config: dict[str, object]) -> None:
        self.config = config

    async def execute(
        self, run: Run, runtime: Runtime
    ) -> AsyncIterator[Event | StrategyOutcome]:
        attempt = await AttemptExecutor(runtime).execute(
            run,
            prompt=str(self.config.get("prompt", run.task.prompt)),
            validator_stages=("after_every_session",),
        )
        yield StrategyOutcome(
            status=RunStatus.COMPLETED if attempt.succeeded else RunStatus.FAILED
        )


@pytest.mark.asyncio
async def test_third_party_strategy_loads_and_uses_shared_lifecycle(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    task = Task(
        id="plugin-strategy",
        name="Plugin strategy",
        prompt="Exercise the plugin contract",
        strategy=StrategyConfig(
            kind="third_party", config={"prompt": "Plugin-owned prompt"}
        ),
        agent=AgentConfig(type="mock", model="mock"),
    )
    entry_point = SimpleNamespace(
        name="third_party", load=lambda: _ThirdPartyStrategy
    )
    runtime.run_validators = AsyncMock(return_value=[])  # type: ignore[method-assign]
    runtime.charge = MagicMock(wraps=runtime.charge)  # type: ignore[method-assign]
    try:
        with patch(
            "horizonx.core.runtime.importlib.metadata.entry_points",
            return_value=[entry_point],
        ):
            run = await runtime.run(task)

        sessions = await store.list_sessions(run.id)
        steps = await store.recent_steps(sessions[0].id, 20)
        assert run.status == RunStatus.COMPLETED
        assert len(sessions) == 1
        assert sessions[0].status.value == "completed"
        assert len(steps) == 3
        runtime.charge.assert_called_once()
        runtime.run_validators.assert_any_await(
            run, sessions[0], when="after_every_session"
        )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_unknown_strategy_fails_before_workspace_preparation(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "horizonx.db")
    runtime = Runtime(store=store, workspace_root=tmp_path / "workspaces")
    runtime.prepare_workspace = AsyncMock()  # type: ignore[method-assign]
    task = Task(
        id="unknown-strategy",
        name="Unknown strategy",
        prompt="Do not materialize a workspace",
        strategy=StrategyConfig(kind="missing_plugin"),
        agent=AgentConfig(type="mock", model="mock"),
    )
    try:
        with patch(
            "horizonx.core.runtime.importlib.metadata.entry_points", return_value=[]
        ):
            with pytest.raises(
                ValueError,
                match="unknown strategy 'missing_plugin'.*available:",
            ):
                await runtime.run(task)

        runtime.prepare_workspace.assert_not_awaited()
    finally:
        await store.close()
