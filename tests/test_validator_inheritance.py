"""Tests for GE-03: validator inheritance from parent nodes."""
from __future__ import annotations

import pytest

from horizonx.core.decomposition_checker import DecompositionChecker
from horizonx.core.goal_graph import GoalGraph
from horizonx.core.types import (
    AgentConfig,
    GoalNode,
    StrategyConfig,
    Task,
    ValidatorConfig,
)


def _task(milestone_validators: list[ValidatorConfig] | None = None) -> Task:
    return Task(
        id="test-inherit",
        name="test",
        prompt="build something",
        strategy=StrategyConfig(kind="single", config={}),
        agent=AgentConfig(type="mock", model="mock"),
        milestone_validators=milestone_validators or [],
    )


def _validator(vid: str, cmd: str = "pytest") -> ValidatorConfig:
    return ValidatorConfig(
        id=vid, type="shell", runs="after_every_session",
        config={"command": cmd},
    )


def _leaf(id: str, parent: str, **kwargs) -> GoalNode:
    return GoalNode(
        id=id, parent_id=parent, name=f"Goal {id}",
        description=f"Description for {id} goal, long enough to pass checks.",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Backward compatibility — task.milestone_validators, no node-level validators
# ---------------------------------------------------------------------------

def test_leaf_with_no_own_validators_inherits_task_milestone_validators():
    task = _task([_validator("tests_pass")])
    graph = GoalGraph.empty("Root", "Root description, long enough.")
    leaf = _leaf("g.root.leaf", "g.root")
    graph.add_child("g.root", leaf)

    effective = graph.effective_validators("g.root.leaf", task.milestone_validators)
    assert [v.id for v in effective] == ["tests_pass"]


def test_root_with_no_own_validators_inherits_task_milestone_validators():
    task = _task([_validator("tests_pass")])
    graph = GoalGraph.empty("Root", "Root description, long enough.")

    effective = graph.effective_validators("g.root", task.milestone_validators)
    assert [v.id for v in effective] == ["tests_pass"]


def test_existing_yaml_with_no_node_validators_unchanged():
    """A graph where NO node sets .validators — every node gets task.milestone_validators."""
    task = _task([_validator("lint"), _validator("tests_pass")])
    graph = GoalGraph.empty("Root", "Root description, long enough.")
    child = _leaf("g.root.child", "g.root")
    graph.add_child("g.root", child)
    grandchild = _leaf("g.root.child.gc", "g.root.child")
    graph.add_child("g.root.child", grandchild)

    for goal_id in ("g.root", "g.root.child", "g.root.child.gc"):
        effective = graph.effective_validators(goal_id, task.milestone_validators)
        assert {v.id for v in effective} == {"lint", "tests_pass"}


# ---------------------------------------------------------------------------
# Node-level validators + inheritance merge
# ---------------------------------------------------------------------------

def test_leaf_with_own_validators_merges_with_inherited():
    task = _task([_validator("tests_pass")])
    graph = GoalGraph.empty("Root", "Root description, long enough.")
    leaf = _leaf("g.root.leaf", "g.root", validators=[_validator("mypy")])
    graph.add_child("g.root", leaf)

    effective = graph.effective_validators("g.root.leaf", task.milestone_validators)
    ids = {v.id for v in effective}
    assert ids == {"tests_pass", "mypy"}


def test_child_overrides_ancestor_validator_by_id():
    task = _task([])
    graph = GoalGraph.empty("Root", "Root description, long enough.")
    graph.root.validators = [_validator("tests_pass", cmd="pytest -q")]
    leaf = _leaf("g.root.leaf", "g.root", validators=[_validator("tests_pass", cmd="pytest -x")])
    graph.add_child("g.root", leaf)

    effective = graph.effective_validators("g.root.leaf", task.milestone_validators)
    assert len(effective) == 1
    assert effective[0].config["command"] == "pytest -x"


def test_three_level_chain_merges_all():
    task = _task([])
    graph = GoalGraph.empty("Root", "Root description, long enough.")
    graph.root.validators = [_validator("root_v")]
    mid = _leaf("g.root.mid", "g.root", validators=[_validator("mid_v")])
    graph.add_child("g.root", mid)
    leaf = _leaf("g.root.mid.leaf", "g.root.mid", validators=[_validator("leaf_v")])
    graph.add_child("g.root.mid", leaf)

    effective = graph.effective_validators("g.root.mid.leaf", task.milestone_validators)
    assert {v.id for v in effective} == {"root_v", "mid_v", "leaf_v"}


# ---------------------------------------------------------------------------
# inherit_validators=False
# ---------------------------------------------------------------------------

def test_inherit_validators_false_disables_inheritance():
    task = _task([_validator("task_v")])
    graph = GoalGraph.empty("Root", "Root description, long enough.")
    graph.root.validators = [_validator("root_v")]
    leaf = _leaf(
        "g.root.leaf", "g.root",
        inherit_validators=False,
        validators=[_validator("own_v")],
    )
    graph.add_child("g.root", leaf)

    effective = graph.effective_validators("g.root.leaf", task.milestone_validators)
    assert [v.id for v in effective] == ["own_v"]


def test_inherit_validators_false_with_no_own_validators_gets_nothing():
    task = _task([_validator("task_v")])
    graph = GoalGraph.empty("Root", "Root description, long enough.")
    leaf = _leaf("g.root.leaf", "g.root", inherit_validators=False)
    graph.add_child("g.root", leaf)

    effective = graph.effective_validators("g.root.leaf", task.milestone_validators)
    assert effective == []


def test_inherit_validators_false_only_affects_that_node():
    """A node with inherit_validators=False doesn't block its own children from inheriting."""
    task = _task([_validator("task_v")])
    graph = GoalGraph.empty("Root", "Root description, long enough.")
    mid = _leaf("g.root.mid", "g.root", inherit_validators=False, validators=[_validator("mid_v")])
    graph.add_child("g.root", mid)
    leaf = _leaf("g.root.mid.leaf", "g.root.mid")  # inherit_validators=True (default)
    graph.add_child("g.root.mid", leaf)

    effective = graph.effective_validators("g.root.mid.leaf", task.milestone_validators)
    # leaf inherits from mid (mid_v), NOT from task_v, since mid sealed off at itself
    assert [v.id for v in effective] == ["mid_v"]


# ---------------------------------------------------------------------------
# GoalNode field defaults + JSON round-trip
# ---------------------------------------------------------------------------

def test_goal_node_validators_default_empty():
    g = GoalNode(id="g.root", name="root", description="root goal")
    assert g.validators == []
    assert g.inherit_validators is True


def test_goal_node_validators_persist_in_json(tmp_path):
    graph = GoalGraph.empty("Task", "A task, long enough description.")
    graph.root.validators = [_validator("tests_pass")]
    graph.root.inherit_validators = False
    save_path = tmp_path / "goals.json"
    graph.save(save_path)
    loaded = GoalGraph.load(save_path)
    assert [v.id for v in loaded.root.validators] == ["tests_pass"]
    assert loaded.root.inherit_validators is False


# ---------------------------------------------------------------------------
# DecompositionChecker integration — NO_CRITERIA should not fire when inheriting
# ---------------------------------------------------------------------------

def test_no_criteria_does_not_fire_when_leaf_inherits_task_validators():
    task = _task([_validator("tests_pass")])
    graph = GoalGraph.empty("Root", "Root description, long enough for checks.")
    leaf = _leaf("g.root.leaf", "g.root", verification_criteria=[])
    graph.add_child("g.root", leaf)

    report = DecompositionChecker().check_graph(graph, task)
    codes = {i.code for i in report.issues}
    assert "NO_CRITERIA" not in codes


def test_no_criteria_still_fires_when_nothing_to_inherit():
    task = _task([])  # no task-level validators either
    graph = GoalGraph.empty("Root", "Root description, long enough for checks.")
    leaf = _leaf("g.root.leaf", "g.root", verification_criteria=[])
    graph.add_child("g.root", leaf)

    report = DecompositionChecker().check_graph(graph, task)
    codes = {i.code for i in report.issues}
    assert "NO_CRITERIA" in codes


def test_no_criteria_does_not_fire_with_own_node_validator():
    task = _task([])
    graph = GoalGraph.empty("Root", "Root description, long enough for checks.")
    leaf = _leaf(
        "g.root.leaf", "g.root",
        verification_criteria=[],
        validators=[_validator("mypy")],
    )
    graph.add_child("g.root", leaf)

    report = DecompositionChecker().check_graph(graph, task)
    codes = {i.code for i in report.issues}
    assert "NO_CRITERIA" not in codes


# ---------------------------------------------------------------------------
# SQLite persistence round-trip (validators / inherit_validators columns)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sqlite_save_and_list_goal_persists_validators(store):
    g = GoalNode(
        id="g.root", name="root", description="root goal",
        validators=[_validator("tests_pass"), _validator("mypy")],
        inherit_validators=False,
    )
    await store.save_goal("run-1", g)
    goals = await store.list_goals("run-1")
    loaded = goals[0]
    assert {v.id for v in loaded.validators} == {"tests_pass", "mypy"}
    assert loaded.inherit_validators is False


@pytest.mark.asyncio
async def test_sqlite_save_and_load_single_goal_persists_validators(store):
    g = GoalNode(
        id="g.root", name="root", description="root goal",
        validators=[_validator("lint")],
    )
    await store.save_goal("run-2", g)
    loaded = await store.load_goal("run-2", "g.root")
    assert loaded is not None
    assert [v.id for v in loaded.validators] == ["lint"]
    assert loaded.inherit_validators is True


@pytest.mark.asyncio
async def test_sqlite_goal_defaults_when_no_validators_set(store):
    g = GoalNode(id="g.root", name="root", description="root goal")
    await store.save_goal("run-3", g)
    goals = await store.list_goals("run-3")
    assert goals[0].validators == []
    assert goals[0].inherit_validators is True


# ---------------------------------------------------------------------------
# Runtime wiring — _effective_validator_configs
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_runtime_effective_validator_configs_no_session_falls_back_to_task(tmp_path):
    from horizonx.core.runtime import Runtime
    from horizonx.core.types import Run
    from horizonx.storage.sqlite import SqliteStore

    store = SqliteStore(tmp_path / "t.db")
    rt = Runtime(store=store, workspace_root=tmp_path / "ws")
    task = _task([_validator("tests_pass")])
    run = Run(task=task, workspace_path=tmp_path / "ws" / "run1")
    configs = rt._effective_validator_configs(run, None)
    assert [c.id for c in configs] == ["tests_pass"]


@pytest.mark.asyncio
async def test_runtime_effective_validator_configs_uses_goal_graph(tmp_path):
    from horizonx.core.runtime import Runtime
    from horizonx.core.types import Run, Session
    from horizonx.storage.sqlite import SqliteStore

    store = SqliteStore(tmp_path / "t.db")
    rt = Runtime(store=store, workspace_root=tmp_path / "ws")
    task = _task([_validator("task_v")])
    workspace = tmp_path / "ws" / "run2"
    workspace.mkdir(parents=True)
    run = Run(task=task, workspace_path=workspace)

    graph = GoalGraph.empty("Root", "Root description, long enough.")
    leaf = _leaf("g.root.leaf", "g.root", validators=[_validator("node_v")])
    graph.add_child("g.root", leaf)
    graph.save(workspace / "goals.json")

    session = Session(run_id=run.id, sequence_index=0, target_goal_id="g.root.leaf")
    configs = rt._effective_validator_configs(run, session)
    assert {c.id for c in configs} == {"task_v", "node_v"}


@pytest.mark.asyncio
async def test_runtime_effective_validator_configs_no_goals_json_falls_back(tmp_path):
    from horizonx.core.runtime import Runtime
    from horizonx.core.types import Run, Session
    from horizonx.storage.sqlite import SqliteStore

    store = SqliteStore(tmp_path / "t.db")
    rt = Runtime(store=store, workspace_root=tmp_path / "ws")
    task = _task([_validator("task_v")])
    workspace = tmp_path / "ws" / "run3"
    workspace.mkdir(parents=True)
    run = Run(task=task, workspace_path=workspace)

    session = Session(run_id=run.id, sequence_index=0, target_goal_id="g.root.leaf")
    configs = rt._effective_validator_configs(run, session)
    assert [c.id for c in configs] == ["task_v"]
