"""Tests for GoalGraph — hierarchy, cycle detection, status propagation."""

from __future__ import annotations

from pathlib import Path

import pytest

from horizonx.core.goal_graph import GoalGraph, GoalGraphError
from horizonx.core.types import GoalNode, GoalStatus


def _make_node(id: str, parent: str | None = None, children: list[str] | None = None) -> GoalNode:
    return GoalNode(
        id=id,
        parent_id=parent,
        name=id,
        description=f"Goal {id}",
        children=children or [],
    )


class TestGoalGraphBasics:
    def test_empty_graph(self):
        g = GoalGraph.empty("Root task", "Root description")
        assert g.root.id == "g.root"
        assert g.root.status == GoalStatus.PENDING

    def test_add_child(self):
        g = GoalGraph.empty("Root", "desc")
        child = _make_node("g.child1", parent="g.root")
        g.add_child("g.root", child)
        assert "g.child1" in g.root.children
        assert g.get("g.child1") is child

    def test_next_pending_leaf(self):
        g = GoalGraph.empty("Root", "desc")
        g.add_child("g.root", _make_node("g.a", parent="g.root"))
        g.add_child("g.root", _make_node("g.b", parent="g.root"))
        leaf = g.next_pending_leaf()
        assert leaf is not None
        assert leaf.id in ("g.a", "g.b")

    def test_next_pending_leaf_skips_done(self):
        g = GoalGraph.empty("Root", "desc")
        g.add_child("g.root", _make_node("g.a", parent="g.root"))
        g.add_child("g.root", _make_node("g.b", parent="g.root"))
        g.mark_in_progress("g.a", by_session="s1")
        g.mark_done("g.a", by_session="s1")
        leaf = g.next_pending_leaf()
        assert leaf is not None
        assert leaf.id == "g.b"

    def test_next_pending_leaf_none_when_all_done(self):
        g = GoalGraph.empty("Root", "desc")
        g.add_child("g.root", _make_node("g.a", parent="g.root"))
        g.mark_in_progress("g.a", by_session="s1")
        g.mark_done("g.a", by_session="s1")
        # Root auto-propagates to done when all children are done
        assert g.next_pending_leaf() is None

    def test_mark_done(self):
        g = GoalGraph.empty("Root", "desc")
        g.add_child("g.root", _make_node("g.a", parent="g.root"))
        g.mark_in_progress("g.a", by_session="s1")
        g.mark_done("g.a", by_session="s1")
        assert g.get("g.a").status == GoalStatus.DONE

    def test_mark_failed(self):
        g = GoalGraph.empty("Root", "desc")
        g.add_child("g.root", _make_node("g.a", parent="g.root"))
        g.mark_failed("g.a", by_session="s1")
        assert g.get("g.a").status == GoalStatus.FAILED

    def test_mark_in_progress(self):
        g = GoalGraph.empty("Root", "desc")
        g.add_child("g.root", _make_node("g.a", parent="g.root"))
        g.mark_in_progress("g.a", by_session="s1")
        assert g.get("g.a").status == GoalStatus.IN_PROGRESS


class TestCycleDetection:
    def test_no_cycle_in_tree(self):
        g = GoalGraph.empty("Root", "desc")
        g.add_child("g.root", _make_node("g.a", parent="g.root"))
        g.add_child("g.a", _make_node("g.b", parent="g.a"))
        # No error = no cycle

    def test_cycle_detected(self):
        g = GoalGraph.empty("Root", "desc")
        a = _make_node("g.a", parent="g.root", children=["g.b"])
        b = _make_node("g.b", parent="g.a", children=["g.a"])
        g._nodes["g.a"] = a
        g._nodes["g.b"] = b
        g._nodes["g.root"].children.append("g.a")
        with pytest.raises(GoalGraphError, match="cycle"):
            g._validate_structure()


class TestSaveLoad:
    def test_round_trip(self, tmp_path: Path):
        g = GoalGraph.empty("Root", "desc")
        g.add_child("g.root", _make_node("g.a", parent="g.root"))
        g.mark_in_progress("g.a", by_session="s1")
        g.mark_done("g.a", by_session="s1")

        path = tmp_path / "goals.json"
        g.save(path)
        g2 = GoalGraph.load(path)

        assert g2.get("g.a").status == GoalStatus.DONE
        assert "g.a" in g2.root.children


class TestStatusPropagation:
    def test_all_children_done_propagates(self):
        g = GoalGraph.empty("Root", "desc")
        g.add_child("g.root", _make_node("g.a", parent="g.root"))
        g.add_child("g.root", _make_node("g.b", parent="g.root"))
        g.mark_in_progress("g.a", by_session="s1")
        g.mark_done("g.a", by_session="s1")
        g.mark_in_progress("g.b", by_session="s1")
        g.mark_done("g.b", by_session="s1")
        assert g.root.status == GoalStatus.DONE

    def test_subtree_addition(self):
        g = GoalGraph.empty("Root", "desc")
        subtree = [
            _make_node("g.sub1", parent="g.root"),
            _make_node("g.sub2", parent="g.root"),
        ]
        for node in subtree:
            g.add_child("g.root", node)
        assert len(g.root.children) == 2

    def test_stats(self):
        g = GoalGraph.empty("Root", "desc")
        g.add_child("g.root", _make_node("g.a", parent="g.root"))
        g.add_child("g.root", _make_node("g.b", parent="g.root"))
        g.mark_in_progress("g.a", by_session="s1")
        stats = g.stats()
        assert stats["total"] == 3
        assert stats["pending"] == 2
        assert stats["in_progress"] == 1


class TestGoalDependencies:
    def test_dep_satisfied_when_none(self):
        g = GoalGraph.empty("Root", "desc")
        g.add_child("g.root", GoalNode(id="g.a", name="a", description="desc"))
        assert g._deps_satisfied(g.get("g.a"))

    def test_dep_blocks_pending_goal(self):
        g = GoalGraph.empty("Root", "desc")
        g.add_child("g.root", GoalNode(id="g.prereq", name="prereq", description="desc"))
        g.add_child("g.root", GoalNode(id="g.dependent", name="dep", description="desc",
                                       depends_on=["g.prereq"]))
        leaf = g.next_pending_leaf()
        assert leaf is not None
        assert leaf.id == "g.prereq"

    def test_dep_unblocks_after_done(self):
        g = GoalGraph.empty("Root", "desc")
        g.add_child("g.root", GoalNode(id="g.prereq", name="prereq", description="desc"))
        g.add_child("g.root", GoalNode(id="g.dependent", name="dep", description="desc",
                                       depends_on=["g.prereq"]))
        g.mark_in_progress("g.prereq", by_session="s1")
        g.mark_done("g.prereq", by_session="s1")
        leaf = g.next_pending_leaf()
        assert leaf is not None
        assert leaf.id == "g.dependent"

    def test_blocked_goals_listing(self):
        g = GoalGraph.empty("Root", "desc")
        g.add_child("g.root", GoalNode(id="g.prereq", name="prereq", description="desc"))
        g.add_child("g.root", GoalNode(id="g.dependent", name="dep", description="desc",
                                       depends_on=["g.prereq"]))
        blocked = g.blocked_goals()
        assert any(n.id == "g.dependent" for n in blocked)

    def test_version_increments_on_mutation(self):
        g = GoalGraph.empty("Root", "desc")
        g.add_child("g.root", GoalNode(id="g.a", name="a", description="desc"))
        assert g.get("g.a").version == 0
        g.mark_in_progress("g.a", by_session="s1")
        assert g.get("g.a").version == 1
        g.mark_done("g.a", by_session="s1")
        assert g.get("g.a").version == 2

    def test_progress_pct_set_on_done(self):
        g = GoalGraph.empty("Root", "desc")
        g.add_child("g.root", GoalNode(id="g.a", name="a", description="desc"))
        g.mark_in_progress("g.a", by_session="s1")
        g.mark_done("g.a", by_session="s1")
        assert g.get("g.a").progress_pct == 100.0

    def test_update_progress(self):
        g = GoalGraph.empty("Root", "desc")
        g.add_child("g.root", GoalNode(id="g.a", name="a", description="desc"))
        g.mark_in_progress("g.a", by_session="s1")
        g.update_progress("g.a", 45.0, by_session="s1")
        assert g.get("g.a").progress_pct == 45.0
        assert g.get("g.a").version == 2

    def test_progress_pct_clamped(self):
        g = GoalGraph.empty("Root", "desc")
        g.add_child("g.root", GoalNode(id="g.a", name="a", description="desc"))
        g.update_progress("g.a", 150.0, by_session="s1")
        assert g.get("g.a").progress_pct == 100.0
        g.update_progress("g.a", -10.0, by_session="s1")
        assert g.get("g.a").progress_pct == 0.0

    def test_notes_version_bump(self):
        g = GoalGraph.empty("Root", "desc")
        g.add_child("g.root", GoalNode(id="g.a", name="a", description="desc"))
        assert g.get("g.a").version == 0
        g.update_notes("g.a", "new notes", by_session="s1")
        assert g.get("g.a").version == 1
        g.append_notes("g.a", "more notes", by_session="s1")
        assert g.get("g.a").version == 2

    def test_goal_node_serialization_with_new_fields(self, tmp_path: Path):
        g = GoalGraph.empty("Root", "desc")
        g.add_child("g.root", GoalNode(
            id="g.a", name="a", description="desc",
            depends_on=["g.root"], progress_pct=50.0, version=3,
        ))
        path = tmp_path / "goals.json"
        g.save(path)
        g2 = GoalGraph.load(path)
        node = g2.get("g.a")
        assert node.depends_on == ["g.root"]
        assert node.progress_pct == 50.0
        assert node.version == 3
