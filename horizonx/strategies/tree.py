"""TreeOfTrials — parallel speculative branching strategy.

Spawns N parallel agent sessions from the same starting point, evaluates
each branch with a scorer, and selects the best outcome (beam search-like).
Useful when the solution space is broad and a single agent might miss the
best path.

Architecture:
  Round 0: Spawn `width` sessions in parallel from the current workspace.
  Each session works independently in its own temporary directory.
  Scorer evaluates all branches; the winner's workspace is merged back.
  If best_score < accept_threshold, repeat up to `max_depth` rounds.

See docs/LONG_HORIZON_AGENT.md §21.5.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from horizonx.agents.base import CancelToken, Workspace
from horizonx.core.event_bus import Event
from horizonx.core.types import AgentConfig, Run, SessionStatus, Step


def _build_agent(ac: AgentConfig) -> Any:
    if ac.type == "claude_code":
        from horizonx.agents.claude_code import ClaudeCodeAgent
        return ClaudeCodeAgent(ac)
    if ac.type == "codex":
        from horizonx.agents.codex import CodexAgent
        return CodexAgent(ac)
    if ac.type == "custom":
        from horizonx.agents.custom import CustomAgent
        return CustomAgent(ac)
    if ac.type == "mock":
        from horizonx.agents.mock import MockAgent
        return MockAgent(config=ac)
    raise ValueError(f"unknown agent type for TreeOfTrials: {ac.type}")


class TreeOfTrials:
    kind = "tree"

    def __init__(self, config: dict[str, Any]):
        self.width: int = config.get("width", 3)
        self.max_depth: int = config.get("max_depth", 2)
        self.accept_threshold: float = config.get("accept_threshold", 0.85)
        self.scorer_command: str | None = config.get("scorer_command")
        self.scorer_type: str = config.get("scorer_type", "shell")  # shell | llm
        self.scorer_model: str = config.get("scorer_model", "claude-haiku-4-5")
        self.prune_below: float = config.get("prune_below", 0.0)

    async def execute(self, run: Run, rt: Any) -> AsyncIterator[Event]:
        yield Event(type="run.started", run_id=run.id, payload={
            "strategy": "tree", "width": self.width, "max_depth": self.max_depth,
        })

        current_workspace = run.workspace_path
        best_score: float = 0.0

        for depth in range(self.max_depth):
            # Spawn width branches in parallel
            branch_tasks = []
            branch_dirs: list[Path] = []

            for branch_idx in range(self.width):
                branch_dir = Path(tempfile.mkdtemp(prefix=f"hx-tree-d{depth}-b{branch_idx}-"))
                if any(current_workspace.iterdir()):
                    shutil.copytree(current_workspace, branch_dir, dirs_exist_ok=True)
                branch_dirs.append(branch_dir)
                branch_tasks.append(self._run_branch(run, rt, branch_dir, branch_idx, depth))

            results = await asyncio.gather(*branch_tasks, return_exceptions=True)

            # Score all branches
            scores: list[float] = []
            for i, (branch_dir, result) in enumerate(zip(branch_dirs, results)):
                if isinstance(result, Exception):
                    scores.append(0.0)
                    continue
                score = await self._score_branch(branch_dir, run)
                scores.append(score)
                yield Event(type="step.recorded", run_id=run.id, payload={
                    "depth": depth, "branch": i, "score": score,
                })

            # Select winner (prune below threshold)
            viable = [(s, d) for s, d in zip(scores, branch_dirs) if s >= self.prune_below]
            if not viable:
                viable = [(scores[0], branch_dirs[0])]  # fallback: keep first
            best_score, winner_dir = max(viable, key=lambda x: x[0])

            # Merge winner back into main workspace
            shutil.copytree(winner_dir, current_workspace, dirs_exist_ok=True)

            # Cleanup other branches
            for d in branch_dirs:
                if d != winner_dir:
                    shutil.rmtree(d, ignore_errors=True)
            shutil.rmtree(winner_dir, ignore_errors=True)

            yield Event(type="goal.in_progress", run_id=run.id, payload={
                "depth": depth, "best_score": best_score, "scores": scores,
            })

            if best_score >= self.accept_threshold:
                break

        await rt.run_validators(run, None, when="final")
        yield Event(type="run.completed", run_id=run.id, payload={
            "strategy": "tree", "depth": depth + 1,
            "final_score": best_score, "width": self.width,
        })

    async def _run_branch(
        self, run: Run, rt: Any, branch_dir: Path, branch_idx: int, depth: int
    ) -> None:
        session = await rt.start_session(run, target_goal=None)
        agent = _build_agent(run.task.agent)
        workspace = Workspace(path=branch_dir, env={})
        cancel = CancelToken()

        prompt = (
            f"Tree-of-trials exploration — depth {depth + 1}, branch {branch_idx + 1}.\n\n"
            f"{run.task.prompt}"
        )

        async def on_step(step: Step, s=session) -> None:
            step.session_id = s.id
            await rt.record_step(s, step)

        result = await agent.run_session(
            prompt, workspace, on_step=on_step,
            cancel_token=cancel, session_id=session.id,
        )
        if result.agent_session_id:
            session.agent_session_id = result.agent_session_id
        await rt.end_session(session, result.status or SessionStatus.COMPLETED)

    async def _score_branch(self, branch_dir: Path, run: Run) -> float:
        if self.scorer_type == "shell" and self.scorer_command:
            return await self._shell_score(branch_dir)
        if self.scorer_type == "llm":
            return await self._llm_score(branch_dir, run)
        return 0.5

    async def _shell_score(self, branch_dir: Path) -> float:
        import re
        proc = await asyncio.create_subprocess_shell(
            self.scorer_command,
            cwd=str(branch_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        text = stdout.decode().strip()
        nums = re.findall(r"[-+]?\d*\.?\d+", text)
        if nums:
            try:
                return max(0.0, min(1.0, float(nums[-1])))
            except ValueError:
                pass
        return 1.0 if proc.returncode == 0 else 0.0

    async def _llm_score(self, branch_dir: Path, run: Run) -> float:
        from horizonx.core.llm_client import call_llm_json
        files_summary = self._summarize_workspace(branch_dir)
        result = await call_llm_json(
            system=(
                "Score the following workspace on a 0.0-1.0 scale against the goal. "
                "Output JSON: {\"score\": 0.0-1.0, \"reason\": \"...\"}"
            ),
            user_prompt=f"GOAL:\n{run.task.prompt[:800]}\n\nWORKSPACE:\n{files_summary}",
            model=self.scorer_model,
            max_tokens=256,
        )
        try:
            return max(0.0, min(1.0, float(result.get("score", 0.5))))
        except (TypeError, ValueError):
            return 0.5

    def _summarize_workspace(self, ws: Path, max_chars: int = 4000) -> str:
        lines: list[str] = []
        total = 0
        for p in sorted(ws.rglob("*")):
            if p.is_dir() or p.name.startswith("."):
                continue
            try:
                chunk = f"\n### {p.relative_to(ws)}\n{p.read_text(errors='replace')[:500]}"
                if total + len(chunk) > max_chars:
                    break
                lines.append(chunk)
                total += len(chunk)
            except OSError:
                continue
        return "".join(lines) or "(empty)"
