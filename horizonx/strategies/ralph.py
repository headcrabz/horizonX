"""RalphLoop — Karpathy autoresearch pattern.

Time-boxed iterative optimization. Agent edits a mutable surface, runs a benchmark,
keeps if metric improves, discards otherwise. See §21.3.
"""

from __future__ import annotations

import asyncio
import shlex
import subprocess
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from horizonx.agents.base import CancelToken, Workspace
from horizonx.agents.claude_code import ClaudeCodeAgent
from horizonx.agents.codex import CodexAgent
from horizonx.core.event_bus import Event
from horizonx.core.types import Run, SessionStatus


def _build_agent(ac: Any):
    if ac.type == "claude_code":
        return ClaudeCodeAgent(ac)
    if ac.type == "codex":
        return CodexAgent(ac)
    if ac.type == "custom":
        from horizonx.agents.custom import CustomAgent
        return CustomAgent(ac)
    if ac.type == "mock":
        from horizonx.agents.mock import MockAgent
        return MockAgent(config=ac)
    raise ValueError(f"unknown agent type for RalphLoop: {ac.type}")


@dataclass
class IterationResult:
    index: int
    metric: float | None
    kept: bool
    elapsed_s: float
    error: str | None = None


RALPH_PROMPT_TEMPLATE = """\
You are running iteration {iter_index} of a Ralph-loop optimization.

CURRENT METRIC: {current_metric}  (direction: {direction})
ITERATIONS REMAINING: ~{iters_left}
TIME BUDGET REMAINING: ~{minutes_left} minutes

YOU MAY ONLY MODIFY THESE FILES:
{mutable_paths}

DO NOT modify any other files. The harness will revert any foreign changes.

PROCESS:
  1. Read recent progress.md and decisions.jsonl to see what's been tried.
  2. Pick ONE specific change (architecture, hyperparameter, optimizer, etc.) likely to improve the metric.
  3. Make the change and append your hypothesis + rationale to decisions.jsonl.
  4. Stop. The harness will run the benchmark and decide whether to keep your change.

USER'S BASE INSTRUCTIONS:
{user_prompt}
"""


class RalphLoop:
    kind = "ralph"

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.fixed_minutes_per_iter: float = config.get("fixed_minutes_per_iter", 5.0)
        self.total_minutes: float = config.get("total_minutes", 600.0)
        self.mutable_paths: list[str] = config.get("mutable_paths", [])
        self.metric_command: str = config.get("metric", {}).get(
            "measurement", "echo 'no measurement command configured'"
        )
        self.metric_name: str = config.get("metric", {}).get("name", "metric")
        self.metric_direction: str = config.get("metric", {}).get("direction", "minimize")
        self.early_stop_window: int = config.get("early_stopping", {}).get("window", 10)
        self.early_stop_delta: float = config.get("early_stopping", {}).get("delta", 0.001)

    async def execute(self, run: Run, rt: Any) -> AsyncIterator[Event]:
        workspace = run.workspace_path
        self._git_init(workspace)

        # Baseline
        baseline = await self._measure(workspace)
        best = baseline
        history: list[float | None] = [baseline]
        yield Event(
            type="run.started",
            run_id=run.id,
            payload={"strategy": "ralph", "baseline": baseline, "metric": self.metric_name},
        )

        start = time.monotonic()
        iter_index = 0
        while (time.monotonic() - start) < self.total_minutes * 60:
            iter_index += 1
            iters_left = int(
                (self.total_minutes * 60 - (time.monotonic() - start)) / max(1, self.fixed_minutes_per_iter * 60)
            )
            minutes_left = round((self.total_minutes * 60 - (time.monotonic() - start)) / 60, 1)
            session = await rt.start_session(run, target_goal=None)

            prompt = RALPH_PROMPT_TEMPLATE.format(
                iter_index=iter_index,
                current_metric=best,
                direction=self.metric_direction,
                iters_left=iters_left,
                minutes_left=minutes_left,
                mutable_paths="\n".join(f"  - {p}" for p in self.mutable_paths) or "  (any)",
                user_prompt=run.task.prompt,
            )

            agent = _build_agent(run.task.agent)
            cancel_token = CancelToken()

            async def on_step(step):
                step.session_id = session.id
                await rt.record_step(session, step)

            ws = Workspace(path=workspace, env={})

            try:
                result = await asyncio.wait_for(
                    agent.run_session(
                        session_prompt=prompt,
                        workspace=ws,
                        on_step=on_step,
                        cancel_token=cancel_token,
                        session_id=session.id,
                    ),
                    timeout=self.fixed_minutes_per_iter * 60 * 1.5,
                )
                if result.agent_session_id:
                    session.agent_session_id = result.agent_session_id
            except asyncio.TimeoutError:
                cancel_token.cancel("iteration timeout")
                result = None

            await rt.end_session(session, SessionStatus.COMPLETED)

            # Verify only mutable paths were touched
            foreign_changes = self._verify_mutable_paths(workspace)
            if foreign_changes:
                self._git_reset_hard(workspace)
                yield Event(
                    type="retry.attempted",
                    run_id=run.id,
                    payload={
                        "iter": iter_index,
                        "reason": "foreign_files_changed",
                        "files": foreign_changes,
                    },
                )
                continue

            # Measure
            metric = await self._measure(workspace)
            history.append(metric)
            kept = self._improves(metric, best)
            if kept:
                best = metric
                self._git_commit(workspace, f"ralph iter {iter_index}: {metric}")
            else:
                self._git_reset_hard(workspace)

            ir = IterationResult(index=iter_index, metric=metric, kept=kept, elapsed_s=time.monotonic() - start)
            yield Event(
                type="goal.in_progress",
                run_id=run.id,
                payload={"iter": ir.index, "metric": ir.metric, "kept": ir.kept, "best": best},
            )

            # Early stop
            if self._should_early_stop(history):
                yield Event(
                    type="run.completed",
                    run_id=run.id,
                    payload={"reason": "early_stop_plateau", "iterations": iter_index, "best": best},
                )
                return

        yield Event(
            type="run.completed",
            run_id=run.id,
            payload={"reason": "time_budget_exhausted", "iterations": iter_index, "best": best},
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _git_init(self, workspace: Path) -> None:
        if not (workspace / ".git").exists():
            subprocess.run(["git", "init"], cwd=workspace, check=False, capture_output=True)
            subprocess.run(["git", "config", "user.email", "horizonx@local"], cwd=workspace, check=False, capture_output=True)
            subprocess.run(["git", "config", "user.name", "HorizonX"], cwd=workspace, check=False, capture_output=True)
            subprocess.run(["git", "add", "-A"], cwd=workspace, check=False, capture_output=True)
            subprocess.run(["git", "commit", "-m", "ralph baseline", "--allow-empty"], cwd=workspace, check=False, capture_output=True)

    def _git_commit(self, workspace: Path, message: str) -> None:
        subprocess.run(["git", "add", "-A"], cwd=workspace, check=False, capture_output=True)
        subprocess.run(["git", "commit", "-m", message, "--allow-empty"], cwd=workspace, check=False, capture_output=True)

    def _git_reset_hard(self, workspace: Path) -> None:
        subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=workspace, check=False, capture_output=True)
        subprocess.run(["git", "clean", "-fd"], cwd=workspace, check=False, capture_output=True)

    def _verify_mutable_paths(self, workspace: Path) -> list[str]:
        if not self.mutable_paths:
            return []
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
        )
        changed = [f.strip() for f in result.stdout.splitlines() if f.strip()]
        foreign = [f for f in changed if not any(self._matches(f, mp) for mp in self.mutable_paths)]
        return foreign

    @staticmethod
    def _matches(path: str, pattern: str) -> bool:
        from fnmatch import fnmatch

        return fnmatch(path, pattern) or fnmatch(path, pattern.rstrip("/") + "/*")

    async def _measure(self, workspace: Path) -> float | None:
        try:
            proc = await asyncio.create_subprocess_shell(
                self.metric_command,
                cwd=str(workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.fixed_minutes_per_iter * 60 * 1.2
            )
            text = (stdout or b"").decode().strip()
            return self._parse_metric(text)
        except Exception:
            return None

    def _parse_metric(self, text: str) -> float | None:
        # Expect last numeric token in stdout to be the metric value.
        import re

        nums = re.findall(r"-?\d+\.?\d*", text)
        if not nums:
            return None
        try:
            return float(nums[-1])
        except ValueError:
            return None

    def _improves(self, new: float | None, best: float | None) -> bool:
        if new is None:
            return False
        if best is None:
            return True
        if self.metric_direction == "minimize":
            return new < best
        return new > best

    def _should_early_stop(self, history: list[float | None]) -> bool:
        clean = [h for h in history[-self.early_stop_window:] if h is not None]
        if len(clean) < self.early_stop_window:
            return False
        return (max(clean) - min(clean)) < self.early_stop_delta
