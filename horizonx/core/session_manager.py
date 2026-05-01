"""SessionManager — composes session prompts and enforces cleanup discipline.

See docs/LONG_HORIZON_AGENT.md §13.
"""

from __future__ import annotations

import json
from pathlib import Path

from horizonx.core.goal_graph import GoalGraph
from horizonx.core.types import GoalNode, Run, ResourceLimits

SESSION_PROMPT_TEMPLATE = """\
You are working on: {task_name}

CURRENT SUB-GOAL:
  ID: {goal_id}
  Name: {goal_name}
  Description: {goal_description}
  Verification criteria:
{verification_criteria}

REQUIRED SESSION STARTUP CHECKLIST (run before any new work):
  1. Run: pwd
  2. Read: progress.md
  3. Read: goals.json
  4. Read: failures.jsonl (filter by your goal_id)
  5. Run: git log --oneline -20
  6. Run: ./init.sh   (starts dev environment if applicable)
  7. Test current functionality is NOT broken before changes

REQUIRED SESSION CLEANUP (run before you finish):
  1. Write: summary.md  (1-page distillation of this session)
  2. Run: git add -A && git commit -m "<descriptive>"
  3. Append your work to: progress.md
  4. Append decisions to: decisions.jsonl
  5. Append failures to: failures.jsonl  (anything you tried that did not work)
  6. Update goals.json `notes` field for YOUR sub-goal
  7. ONLY if verification criteria are met, propose status="done" in your final message.

DISCIPLINE:
  - You may modify only the `notes` field and propose `status` for YOUR sub-goal in goals.json.
  - It is unacceptable to delete or edit existing tests.
  - You may NOT mark goals `done` directly — that is the Runtime's job after validators pass.
  - You may NOT modify other sub-goals' fields.

CONTEXT FROM PREVIOUS SESSIONS:
========== summary.md ==========
{summary_md}
========== last 80 lines of progress.md ==========
{progress_tail}
========== last 20 entries of decisions.jsonl ==========
{decisions_tail}
========== failures.jsonl entries for this goal ==========
{failures_for_goal}
========== git log --oneline -20 ==========
{git_log}

LIMITS FOR THIS SESSION:
  Max steps: {max_steps}
  Max minutes: {max_minutes}
  When approaching limits, finish cleanly: write summary.md, commit, update progress.md, then stop.

YOUR INITIAL INSTRUCTIONS:
{user_prompt}
"""


class SessionManager:
    """Composes session prompts and enforces cleanup protocol."""

    def __init__(self, run: Run):
        self.run = run
        self.workspace = run.workspace_path

    def compose_prompt(self, target_goal: GoalNode | None) -> str:
        if target_goal is None:
            return self._compose_initializer_prompt()

        graph = self._load_goal_graph()
        return SESSION_PROMPT_TEMPLATE.format(
            task_name=self.run.task.name,
            goal_id=target_goal.id,
            goal_name=target_goal.name,
            goal_description=target_goal.description,
            verification_criteria="\n".join(f"    - {c}" for c in target_goal.verification_criteria) or "    (none)",
            summary_md=self._read_or_default("summary.md", "(no previous summary)"),
            progress_tail=self._tail("progress.md", 80),
            decisions_tail=self._tail_jsonl("decisions.jsonl", 20),
            failures_for_goal=self._failures_for_goal(target_goal.id),
            git_log=self._git_log(),
            max_steps=self.run.task.resources.max_steps_per_session,
            max_minutes=self.run.task.resources.max_minutes_per_session,
            user_prompt=self.run.task.prompt,
        )

    def _compose_initializer_prompt(self) -> str:
        return f"""\
You are the INITIALIZER for a long-horizon task. Your single job in THIS session:

1. Read the user's task description below.
2. Decompose it into a hierarchical goal graph and write `goals.json` per the schema below.
3. Create `init.sh` that bootstraps the environment so subsequent sessions can run it as step 1 of their checklist.
4. Create `progress.md` with a "Session 1 — Initializer" entry summarizing what you did.
5. Make an initial git commit so the workspace has clean baseline state.
6. DO NOT implement the task itself. That is for subsequent sessions.

GOAL GRAPH SCHEMA (goals.json):
{{
  "version": 1,
  "root": "g.root",
  "nodes": {{
    "g.root": {{
      "name": "...",
      "description": "...",
      "children": ["g.subgoal_a", "g.subgoal_b"],
      "verification_criteria": ["..."],
      "status": "pending",
      "attempts": 0,
      "notes": ""
    }},
    "g.subgoal_a": {{
      "parent_id": "g.root",
      "name": "...",
      "description": "...",
      "verification_criteria": ["..."],
      "children": [],
      "status": "pending",
      "attempts": 0,
      "max_attempts": 3,
      "notes": ""
    }}
  }}
}}

DESIGN GUIDELINES:
  - Aim for 5–80 leaf goals (atomic units, each fittable in one ~25 minute session).
  - Every leaf has clear, testable verification_criteria.
  - Goals form a DAG — no cycles, no orphans.
  - Use slugged ids: "g.<area>.<feature>".

USER'S TASK:
{self.run.task.prompt}
"""

    # ------------------------------------------------------------------
    # File helpers
    # ------------------------------------------------------------------

    def _load_goal_graph(self) -> GoalGraph | None:
        path = self.workspace / "goals.json"
        if not path.exists():
            return None
        return GoalGraph.load(path)

    def _read_or_default(self, name: str, default: str) -> str:
        p = self.workspace / name
        if not p.exists():
            return default
        return p.read_text()

    def _tail(self, name: str, lines: int) -> str:
        p = self.workspace / name
        if not p.exists():
            return f"({name} not yet created)"
        text = p.read_text().splitlines()
        return "\n".join(text[-lines:])

    def _tail_jsonl(self, name: str, n: int) -> str:
        p = self.workspace / name
        if not p.exists():
            return f"({name} not yet created)"
        lines = p.read_text().splitlines()[-n:]
        return "\n".join(lines)

    def _failures_for_goal(self, goal_id: str) -> str:
        p = self.workspace / "failures.jsonl"
        if not p.exists():
            return "(no recorded failures)"
        out = []
        for line in p.read_text().splitlines():
            try:
                obj = json.loads(line)
                if obj.get("goal") == goal_id:
                    out.append(line)
            except json.JSONDecodeError:
                continue
        return "\n".join(out) if out else "(no failures recorded for this goal)"

    def _git_log(self) -> str:
        import subprocess

        try:
            out = subprocess.run(
                ["git", "log", "--oneline", "-20"],
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=5,
            )
            return out.stdout or "(no git history yet)"
        except Exception:
            return "(git unavailable)"
