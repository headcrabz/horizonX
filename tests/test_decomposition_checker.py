"""Tests for GE-01: decomposition quality pre-check."""
from __future__ import annotations

from horizonx.core.decomposition_checker import DecompositionChecker
from horizonx.core.goal_graph import GoalGraph
from horizonx.core.types import GoalNode, GoalStatus, Task

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _task() -> Task:
    from horizonx.core.types import AgentConfig, StrategyConfig
    return Task(
        id="test-decomp",
        name="test",
        prompt="build something",
        strategy=StrategyConfig(kind="single", config={}),
        agent=AgentConfig(type="mock", model="mock"),
    )


def _graph_with_node(
    name: str,
    description: str = "A concrete, verifiable description for this goal.",
    criteria: list[str] | None = None,
    children: list[str] | None = None,
) -> GoalGraph:
    g = GoalGraph.empty(name, description)
    g.root.verification_criteria = criteria or ["tests pass", "no regressions"]
    g.root.children = children or []
    return g


# ---------------------------------------------------------------------------
# No issues — clean graph
# ---------------------------------------------------------------------------

def test_clean_graph_returns_no_errors():
    """A well-formed graph has no errors (may have SINGLE_NODE info only)."""
    graph = _graph_with_node("Implement JWT token endpoint")
    report = DecompositionChecker().check_graph(graph, _task())
    assert not report.has_errors
    assert report.errors == []
    assert report.warnings == []


# ---------------------------------------------------------------------------
# VAGUE_NAME
# ---------------------------------------------------------------------------

def test_vague_name_triggers_warning():
    graph = _graph_with_node("improve the authentication system")
    report = DecompositionChecker().check_graph(graph, _task())
    codes = {i.code for i in report.issues}
    assert "VAGUE_NAME" in codes


def test_specific_name_no_warning():
    graph = _graph_with_node("Add rate limiting middleware with 429 responses")
    report = DecompositionChecker().check_graph(graph, _task())
    codes = {i.code for i in report.issues}
    assert "VAGUE_NAME" not in codes


def test_short_name_triggers_warning():
    graph = _graph_with_node("fix it")
    report = DecompositionChecker().check_graph(graph, _task())
    codes = {i.code for i in report.issues}
    assert "SHORT_NAME" in codes or "VAGUE_NAME" in codes


# ---------------------------------------------------------------------------
# NO_DESCRIPTION
# ---------------------------------------------------------------------------

def test_empty_description_triggers_warning():
    graph = _graph_with_node("Implement JWT token endpoint", description="")
    report = DecompositionChecker().check_graph(graph, _task())
    codes = {i.code for i in report.issues}
    assert "NO_DESCRIPTION" in codes


def test_short_description_triggers_warning():
    graph = _graph_with_node("Implement JWT token endpoint", description="do it")
    report = DecompositionChecker().check_graph(graph, _task())
    codes = {i.code for i in report.issues}
    assert "NO_DESCRIPTION" in codes


# ---------------------------------------------------------------------------
# NO_CRITERIA
# ---------------------------------------------------------------------------

def test_leaf_with_no_criteria_warns():
    # Build a 2-node graph: root (has children) + leaf (no criteria)
    graph = GoalGraph.empty("Add rate limiting system", "Comprehensive description for the root goal.")
    graph.root.verification_criteria = ["all sub-goals done"]
    leaf = GoalNode(
        id="g.root.middleware",
        parent_id="g.root",
        name="Add rate limiting middleware",
        description="Add rate limiting middleware to the API gateway returning 429.",
        verification_criteria=[],  # intentionally empty — should warn
    )
    graph.add_child("g.root", leaf)
    report = DecompositionChecker().check_graph(graph, _task())
    codes = {i.code for i in report.issues}
    assert "NO_CRITERIA" in codes


def test_leaf_with_criteria_no_warning():
    graph = _graph_with_node("Add rate limiting", criteria=["pytest passes"])
    report = DecompositionChecker().check_graph(graph, _task())
    codes = {i.code for i in report.issues}
    assert "NO_CRITERIA" not in codes


# ---------------------------------------------------------------------------
# TOO_MANY_CHILDREN
# ---------------------------------------------------------------------------

def test_too_many_children_warns():
    graph = GoalGraph.empty("Implement comprehensive authentication", "Root task with many children.")
    graph.root.verification_criteria = ["all children done"]
    # Add 9 children to root (max is 8)
    for i in range(9):
        child = GoalNode(
            id=f"g.root.child-{i}",
            parent_id="g.root",
            name=f"Implement child component {i} with tests",
            description="A concrete, verifiable description for this child goal node.",
            verification_criteria=["tests pass"],
        )
        graph.add_child("g.root", child)
    report = DecompositionChecker().check_graph(graph, _task())
    codes = {i.code for i in report.issues}
    assert "TOO_MANY_CHILDREN" in codes


# ---------------------------------------------------------------------------
# SINGLE_NODE
# ---------------------------------------------------------------------------

def test_single_node_is_info_not_error():
    graph = _graph_with_node("Implement complete auth system")
    report = DecompositionChecker().check_graph(graph, _task())
    severities = {i.severity for i in report.issues if i.code == "SINGLE_NODE"}
    assert severities == {"info"}
    assert not report.has_errors  # info only, not error


# ---------------------------------------------------------------------------
# FAILED_NO_ATTEMPTS
# ---------------------------------------------------------------------------

def test_failed_goal_with_zero_attempts_warns():
    graph = _graph_with_node("Implement login endpoint")
    graph.root.status = GoalStatus.FAILED
    graph.root.attempts = 0
    report = DecompositionChecker().check_graph(graph, _task())
    codes = {i.code for i in report.issues}
    assert "FAILED_NO_ATTEMPTS" in codes


# ---------------------------------------------------------------------------
# to_dict
# ---------------------------------------------------------------------------

def test_to_dict_structure():
    graph = _graph_with_node("improve something", description="")
    report = DecompositionChecker().check_graph(graph, _task())
    d = report.to_dict()
    assert "error_count" in d
    assert "warning_count" in d
    assert "issues" in d
    assert isinstance(d["issues"], list)
    for issue in d["issues"]:
        assert "severity" in issue
        assert "code" in issue
        assert "message" in issue


# ---------------------------------------------------------------------------
# check_file — missing file returns empty report
# ---------------------------------------------------------------------------

def test_check_file_missing_returns_empty(tmp_path):
    report = DecompositionChecker().check_file(tmp_path / "goals.json", _task())
    assert report.ok


def test_check_file_loads_and_checks(tmp_path):
    graph = _graph_with_node("improve something", criteria=[])
    graph.save(tmp_path / "goals.json")
    report = DecompositionChecker().check_file(tmp_path / "goals.json", _task())
    assert len(report.issues) > 0


# ---------------------------------------------------------------------------
# Multiple issues on the same node
# ---------------------------------------------------------------------------

def test_multiple_issues_on_vague_undescribed_leaf():
    graph = _graph_with_node("fix it", description="ok", criteria=[])
    report = DecompositionChecker().check_graph(graph, _task())
    codes = {i.code for i in report.issues}
    # short name + bad description + no criteria
    assert len(codes) >= 2
