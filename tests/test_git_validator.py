"""Tests for horizonx.validators.git."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest


class TestGitGate:
    def test_init_defaults(self):
        from horizonx.validators.git import GitGate
        g = GitGate({})
        assert g.min_commits == 0
        assert g.require_clean is False

    @pytest.mark.asyncio
    async def test_passes_on_no_constraints(self, tmp_path: Path):
        from horizonx.validators.git import GitGate

        subprocess.run(["git", "init"], cwd=tmp_path, check=False, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=False, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=False, capture_output=True)
        (tmp_path / "file.txt").write_text("hello")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=False, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=False, capture_output=True)

        g = GitGate({"min_commits": 0})
        workspace = MagicMock()
        workspace.path = tmp_path
        run = MagicMock()

        decision = await g.validate(run, None, workspace)
        assert decision.decision.value == "continue"

    @pytest.mark.asyncio
    async def test_fails_min_commits(self, tmp_path: Path):
        from horizonx.validators.git import GitGate

        subprocess.run(["git", "init"], cwd=tmp_path, check=False, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=False, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=False, capture_output=True)
        (tmp_path / "f.txt").write_text("x")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=False, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=False, capture_output=True)

        g = GitGate({"min_commits": 5, "on_fail": "pause_for_hitl"})
        workspace = MagicMock()
        workspace.path = tmp_path
        run = MagicMock()

        decision = await g.validate(run, None, workspace)
        assert decision.decision.value == "pause_for_hitl"
        assert "1 commit" in decision.reason or "commit(s)" in decision.reason

    @pytest.mark.asyncio
    async def test_revert_detection(self, tmp_path: Path):
        from horizonx.validators.git import GitGate

        subprocess.run(["git", "init"], cwd=tmp_path, check=False, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=False, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=False, capture_output=True)
        (tmp_path / "f.txt").write_text("x")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=False, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=False, capture_output=True)
        (tmp_path / "f.txt").write_text("y")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=False, capture_output=True)
        subprocess.run(["git", "commit", "-m", "revert: undo previous change"], cwd=tmp_path, check=False, capture_output=True)

        g = GitGate({"max_revert_commits": 0, "on_fail": "pause_for_hitl"})
        workspace = MagicMock()
        workspace.path = tmp_path
        run = MagicMock()

        decision = await g.validate(run, None, workspace)
        assert decision.decision.value == "pause_for_hitl"
        assert "revert" in decision.reason
