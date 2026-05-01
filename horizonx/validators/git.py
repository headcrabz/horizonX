"""GitGate — validate workspace git history as a quality signal.

Checks:
  - min_commits: at least N commits have been made (agent actually saved work)
  - no_reverts: no "revert" commits in recent history (spin indicator)
  - files_changed: at least N files were modified vs a base ref
  - no_uncommitted: working tree is clean (no dirty files)

These guards make it harder for agents to fake progress by doing nothing
or by oscillating changes without ever committing.

See docs/LONG_HORIZON_AGENT.md §16.4.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from horizonx.core.types import GateAction, GateDecision, Run, Session


class GitGate:
    name = "git"

    def __init__(self, config: dict[str, Any]):
        self.min_commits: int = config.get("min_commits", 0)
        self.max_revert_commits: int = config.get("max_revert_commits", 0)
        self.min_files_changed: int = config.get("min_files_changed", 0)
        self.base_ref: str = config.get("base_ref", "HEAD~1")
        self.require_clean: bool = config.get("require_clean", False)
        self.on_fail: str = config.get("on_fail", "pause_for_hitl")
        self._id: str = config.get("id", "git")

    async def validate(self, run: Run, session: Session | None, workspace: Any) -> GateDecision:
        start = time.monotonic()
        ws = str(workspace.path)

        commit_count = await self._count_commits(ws)
        revert_count = await self._count_reverts(ws)
        files_changed = await self._count_files_changed(ws)
        is_clean = await self._check_clean(ws)

        failures: list[str] = []

        if self.min_commits > 0 and commit_count < self.min_commits:
            failures.append(
                f"only {commit_count} commit(s), need >= {self.min_commits}"
            )

        if revert_count > self.max_revert_commits:
            failures.append(
                f"{revert_count} revert commit(s), max allowed is {self.max_revert_commits}"
            )

        if self.min_files_changed > 0 and files_changed < self.min_files_changed:
            failures.append(
                f"only {files_changed} file(s) changed vs {self.base_ref}, "
                f"need >= {self.min_files_changed}"
            )

        if self.require_clean and not is_clean:
            failures.append("workspace has uncommitted changes")

        passed = len(failures) == 0
        score = 1.0 if passed else max(0.0, 1.0 - 0.25 * len(failures))

        return GateDecision(
            decision=GateAction.CONTINUE if passed else GateAction(self.on_fail),
            reason="; ".join(failures) if failures else "all git checks passed",
            score=score,
            details={
                "commit_count": commit_count,
                "revert_count": revert_count,
                "files_changed": files_changed,
                "is_clean": is_clean,
            },
            validator_name=self._id,
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    async def _count_commits(self, ws: str) -> int:
        proc = await asyncio.create_subprocess_exec(
            "git", "rev-list", "--count", "HEAD",
            cwd=ws, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        try:
            return int(stdout.decode().strip())
        except ValueError:
            return 0

    async def _count_reverts(self, ws: str) -> int:
        proc = await asyncio.create_subprocess_exec(
            "git", "log", "--oneline", "-50",
            cwd=ws, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        lines = stdout.decode().lower().splitlines()
        return sum(1 for ln in lines if "revert" in ln)

    async def _count_files_changed(self, ws: str) -> int:
        proc = await asyncio.create_subprocess_exec(
            "git", "diff", "--name-only", self.base_ref,
            cwd=ws, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return 0
        return len([ln for ln in stdout.decode().splitlines() if ln.strip()])

    async def _check_clean(self, ws: str) -> bool:
        proc = await asyncio.create_subprocess_exec(
            "git", "status", "--porcelain",
            cwd=ws, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return stdout.decode().strip() == ""
