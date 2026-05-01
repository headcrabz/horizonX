"""Durable hierarchical goal graph.

The single source of truth for what's happening in a run. Stored as
goals.json in the workspace and mirrored to the goals table.
See docs/LONG_HORIZON_AGENT.md §12.
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Iterable

from horizonx.core.types import GoalNode, GoalStatus, utcnow


class GoalGraphError(Exception):
    """Raised on structural violations of the goal graph."""


class GoalGraph:
    """Hierarchical task plan, persistent across sessions.

    Invariants enforced on every mutation:
    - Single root named `g.root`.
    - DAG only (no cycles).
    - Status transitions are monotonic per node.
    - Atomic operations: load → mutate via methods → save.
    """

    ROOT_ID = "g.root"

    def __init__(self, nodes: dict[str, GoalNode]):
        self._nodes = nodes
        self._validate_structure()

    # ------------------------------------------------------------------
    # IO
    # ------------------------------------------------------------------

    @classmethod
    def empty(cls, root_name: str, root_description: str) -> "GoalGraph":
        root = GoalNode(
            id=cls.ROOT_ID,
            name=root_name,
            description=root_description,
            status=GoalStatus.PENDING,
        )
        return cls({cls.ROOT_ID: root})

    @classmethod
    def load(cls, path: Path) -> "GoalGraph":
        data = json.loads(path.read_text())
        nodes = {nid: GoalNode(**n) for nid, n in data["nodes"].items()}
        return cls(nodes)

    def save(self, path: Path) -> None:
        data = {
            "version": 1,
            "root": self.ROOT_ID,
            "nodes": {nid: n.model_dump(mode="json") for nid, n in self._nodes.items()},
        }
        path.write_text(json.dumps(data, indent=2, default=str))

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    @property
    def root(self) -> GoalNode:
        return self._nodes[self.ROOT_ID]

    def get(self, goal_id: str) -> GoalNode:
        if goal_id not in self._nodes:
            raise GoalGraphError(f"unknown goal id: {goal_id}")
        return self._nodes[goal_id]

    def all_nodes(self) -> Iterable[GoalNode]:
        return self._nodes.values()

    def leaves(self) -> list[GoalNode]:
        return [n for n in self._nodes.values() if not n.children]

    def next_pending_leaf(self) -> GoalNode | None:
        """Pick the next leaf to attempt (DFS, pending-first, then in_progress).

        Respects depends_on: a goal is only eligible when all its dependencies are DONE.
        """
        for status in (GoalStatus.IN_PROGRESS, GoalStatus.PENDING):
            for n in self.leaves():
                if n.status == status and n.attempts < n.max_attempts and self._deps_satisfied(n):
                    return n
        return None

    def _deps_satisfied(self, node: GoalNode) -> bool:
        """True if all depends_on goals are DONE."""
        for dep_id in node.depends_on:
            dep = self._nodes.get(dep_id)
            if dep is None or dep.status != GoalStatus.DONE:
                return False
        return True

    def blocked_goals(self) -> list[GoalNode]:
        """Return leaves that are pending but have unsatisfied dependencies."""
        return [
            n for n in self.leaves()
            if n.status == GoalStatus.PENDING and not self._deps_satisfied(n)
        ]

    def is_complete(self) -> bool:
        return self.root.status == GoalStatus.DONE

    def stats(self) -> dict[str, int]:
        out = {s.value: 0 for s in GoalStatus}
        for n in self._nodes.values():
            out[n.status.value] += 1
        out["total"] = len(self._nodes)
        return out

    # ------------------------------------------------------------------
    # Mutations — Runtime-owned (status transitions)
    # ------------------------------------------------------------------

    def mark_in_progress(self, goal_id: str, by_session: str) -> None:
        node = self.get(goal_id)
        if node.status not in (GoalStatus.PENDING, GoalStatus.IN_PROGRESS):
            raise GoalGraphError(
                f"cannot mark in_progress from {node.status.value} for {goal_id}"
            )
        node.status = GoalStatus.IN_PROGRESS
        node.attempts += 1
        node.version += 1
        node.last_updated_at = utcnow()
        node.last_updated_by_session = by_session

    def mark_done(self, goal_id: str, by_session: str) -> None:
        node = self.get(goal_id)
        if node.status not in (GoalStatus.IN_PROGRESS, GoalStatus.PENDING):
            raise GoalGraphError(f"cannot mark done from {node.status.value} for {goal_id}")
        node.status = GoalStatus.DONE
        node.progress_pct = 100.0
        node.version += 1
        node.last_updated_at = utcnow()
        node.last_updated_by_session = by_session
        self._propagate_status_up(node)

    def mark_failed(self, goal_id: str, by_session: str) -> None:
        node = self.get(goal_id)
        node.status = GoalStatus.FAILED
        node.version += 1
        node.last_updated_at = utcnow()
        node.last_updated_by_session = by_session
        self._propagate_status_up(node)

    def mark_blocked(self, goal_id: str) -> None:
        node = self.get(goal_id)
        node.status = GoalStatus.BLOCKED
        node.version += 1

    def update_progress(self, goal_id: str, pct: float, by_session: str) -> None:
        """Update partial progress (0-100) without changing status."""
        node = self.get(goal_id)
        node.progress_pct = max(0.0, min(100.0, pct))
        node.version += 1
        node.last_updated_at = utcnow()
        node.last_updated_by_session = by_session

    # ------------------------------------------------------------------
    # Mutations — Agent-owned (notes only)
    # ------------------------------------------------------------------

    def update_notes(self, goal_id: str, notes: str, by_session: str) -> None:
        node = self.get(goal_id)
        node.notes = notes
        node.version += 1
        node.last_updated_at = utcnow()
        node.last_updated_by_session = by_session

    def append_notes(self, goal_id: str, fragment: str, by_session: str) -> None:
        node = self.get(goal_id)
        sep = "\n\n" if node.notes else ""
        node.notes = f"{node.notes}{sep}{fragment}"
        node.version += 1
        node.last_updated_at = utcnow()
        node.last_updated_by_session = by_session

    # ------------------------------------------------------------------
    # Decomposition (Initializer-owned)
    # ------------------------------------------------------------------

    def add_child(self, parent_id: str, child: GoalNode) -> None:
        if child.id in self._nodes:
            raise GoalGraphError(f"duplicate goal id: {child.id}")
        if parent_id not in self._nodes:
            raise GoalGraphError(f"unknown parent: {parent_id}")
        child.parent_id = parent_id
        self._nodes[child.id] = child
        self._nodes[parent_id].children.append(child.id)
        self._validate_structure()

    def add_subtree(self, parent_id: str, nodes: list[GoalNode]) -> None:
        for n in nodes:
            self.add_child(n.parent_id or parent_id, n)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _propagate_status_up(self, node: GoalNode) -> None:
        """If all children of a parent are done, mark the parent done too.
        If any child is failed, parent stays in_progress unless we explicitly fail it."""
        if not node.parent_id:
            return
        parent = self._nodes[node.parent_id]
        children = [self._nodes[c] for c in parent.children]
        if all(c.status == GoalStatus.DONE for c in children) and parent.status != GoalStatus.DONE:
            parent.status = GoalStatus.DONE
            parent.last_updated_at = utcnow()
            self._propagate_status_up(parent)

    def _validate_structure(self) -> None:
        if self.ROOT_ID not in self._nodes:
            raise GoalGraphError(f"missing root node: {self.ROOT_ID}")
        # DFS cycle check with path-tracking
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {nid: WHITE for nid in self._nodes}

        def dfs(nid: str, path: list[str]) -> None:
            if color[nid] == GRAY:
                raise GoalGraphError(f"cycle detected: {' -> '.join(path + [nid])}")
            if color[nid] == BLACK:
                return
            color[nid] = GRAY
            for child in self._nodes[nid].children:
                if child not in self._nodes:
                    raise GoalGraphError(f"dangling child reference: {child}")
                dfs(child, path + [nid])
            color[nid] = BLACK

        dfs(self.ROOT_ID, [])
        # Orphan check — every non-root node must be reachable from root
        reachable = {nid for nid, c in color.items() if c == BLACK}
        orphans = set(self._nodes) - reachable
        if orphans:
            raise GoalGraphError(f"orphan goal(s): {orphans}")
