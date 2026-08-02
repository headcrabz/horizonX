"""RalphLoop — Karpathy autoresearch pattern.

Time-boxed iterative optimization. Agent edits a mutable surface, runs a benchmark,
keeps if metric improves, discards otherwise. See §21.3.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from horizonx.core.attempt_executor import AttemptExecutor
from horizonx.core.event_bus import Event
from horizonx.core.types import Run, RunStatus, SessionStatus, StrategyOutcome

RALPH_PROMPT_TEMPLATE = """\
Iteration {iter_index} — {iters_left} iterations and {minutes_left} minutes remaining.

Current best {direction} metric: {current_metric}

Mutable paths you may edit:
{mutable_paths}

Task:
{user_prompt}

Make ONE targeted improvement. Do not touch files outside the mutable paths list.
"""


@dataclass
class IterationResult:
    index: int
    metric: float | None
    kept: bool
    elapsed_s: float


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

    async def execute(self, run: Run, rt: Any) -> AsyncIterator[Event | StrategyOutcome]:
        workspace = run.workspace_path
        self._git_init(workspace)

        # Baseline
        baseline = await self._measure(workspace)
        if baseline is None:
            yield StrategyOutcome(
                status=RunStatus.FAILED, reason="baseline_metric_unavailable"
            )
            return
        best = baseline
        history: list[float | None] = [baseline]
        yield Event(
            type="run.started",
            run_id=run.id,
            payload={"strategy": "ralph", "baseline": baseline, "metric": self.metric_name},
        )

        start = time.monotonic()
        iter_index = 0
        completed_iterations = 0
        timed_out_iterations = 0
        while (time.monotonic() - start) < self.total_minutes * 60:
            iter_index += 1
            iters_left = int(
                (self.total_minutes * 60 - (time.monotonic() - start)) / max(1, self.fixed_minutes_per_iter * 60)
            )
            minutes_left = round((self.total_minutes * 60 - (time.monotonic() - start)) / 60, 1)
            prompt = RALPH_PROMPT_TEMPLATE.format(
                iter_index=iter_index,
                current_metric=best,
                direction=self.metric_direction,
                iters_left=iters_left,
                minutes_left=minutes_left,
                mutable_paths="\n".join(f"  - {p}" for p in self.mutable_paths) or "  (any)",
                user_prompt=run.task.prompt,
            )

            attempt = await AttemptExecutor(rt).execute(
                run,
                prompt=prompt,
                workspace_path=workspace,
                timeout_seconds=self.fixed_minutes_per_iter * 60 * 1.5,
            )
            result = attempt.agent

            if attempt.status == SessionStatus.TIMEOUT:
                timed_out_iterations += 1
                yield Event(
                    type="retry.attempted",
                    run_id=run.id,
                    payload={"iter": iter_index, "reason": "iteration_timeout"},
                )
                continue
            if not attempt.succeeded:
                yield StrategyOutcome(
                    status=(
                        RunStatus.TIMED_OUT
                        if result.status == SessionStatus.TIMEOUT
                        else RunStatus.FAILED
                    ),
                    reason=f"agent_{result.status.value}",
                    details={"iteration": iter_index, "error": result.error},
                )
                return
            completed_iterations += 1

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
            if metric is None:
                yield StrategyOutcome(
                    status=RunStatus.FAILED,
                    reason="metric_unavailable",
                    details={"iteration": iter_index, "best": best},
                )
                return
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
                yield StrategyOutcome(
                    status=RunStatus.COMPLETED,
                    reason="early_stop_plateau",
                    details={"iterations": iter_index, "best": best},
                )
                return

        if timed_out_iterations and completed_iterations == 0:
            yield StrategyOutcome(
                status=RunStatus.TIMED_OUT,
                reason="all_iterations_timed_out",
                details={"iterations": iter_index, "best": best},
            )
        else:
            yield StrategyOutcome(
                status=RunStatus.COMPLETED,
                reason="optimization_window_complete",
                details={"iterations": iter_index, "best": best},
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
