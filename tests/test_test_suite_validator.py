"""Tests for horizonx.validators.test_suite."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from horizonx.validators.test_suite import TestSuiteGate, _count_assertions


class TestTestSuiteGateAntiGaming:
    def test_assertion_counting_python(self, tmp_path: Path):
        test_file = tmp_path / "test_foo.py"
        test_file.write_text(textwrap.dedent("""\
            def test_add():
                assert 1 + 1 == 2
                assert 2 + 2 == 4

            def test_multiply():
                assert 3 * 3 == 9
        """))
        count = _count_assertions(tmp_path, "test_*.py")
        assert count == 3

    def test_assertion_counting_empty_tests(self, tmp_path: Path):
        test_file = tmp_path / "test_empty.py"
        test_file.write_text("def test_nothing():\n    pass\n")
        count = _count_assertions(tmp_path, "test_*.py")
        assert count == 0

    def test_assertion_counting_jest(self, tmp_path: Path):
        test_file = tmp_path / "test_app.test.js"
        test_file.write_text(textwrap.dedent("""\
            test('adds', () => {
                expect(1 + 1).toBe(2);
                expect(2 + 2).toBe(4);
            });
        """))
        count = _count_assertions(tmp_path, "*.test.js")
        assert count == 2

    @pytest.mark.asyncio
    async def test_file_count_guard(self, tmp_path: Path):
        gate = TestSuiteGate({
            "command": "true",
            "test_dir": "tests/",
            "test_glob": "test_*.py",
            "min_test_count": 3,
        })
        workspace = MagicMock()
        workspace.path = tmp_path
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_one.py").write_text("def test_x(): assert True")

        run = MagicMock()
        decision = await gate.validate(run, None, workspace)
        assert decision.score == 0.0
        assert "test file count" in decision.reason

    @pytest.mark.asyncio
    async def test_assertion_count_guard(self, tmp_path: Path):
        gate = TestSuiteGate({
            "command": "true",
            "test_dir": "tests/",
            "test_glob": "test_*.py",
            "min_assertion_count": 10,
        })
        workspace = MagicMock()
        workspace.path = tmp_path
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_stub.py").write_text("def test_x():\n    pass\n")

        run = MagicMock()
        decision = await gate.validate(run, None, workspace)
        assert decision.score == 0.0
        assert "assertion count" in decision.reason

    @pytest.mark.asyncio
    async def test_exec_time_floor(self, tmp_path: Path):
        gate = TestSuiteGate({
            "command": "true",
            "test_dir": "tests/",
            "test_glob": "test_*.py",
            "min_exec_seconds": 999.0,
        })
        workspace = MagicMock()
        workspace.path = tmp_path
        (tmp_path / "tests").mkdir()

        run = MagicMock()
        decision = await gate.validate(run, None, workspace)
        assert decision.score == 0.0
        assert "min" in decision.reason and "999" in decision.reason

    @pytest.mark.asyncio
    async def test_passing_suite(self, tmp_path: Path):
        gate = TestSuiteGate({
            "command": "true",
            "test_dir": "tests/",
            "test_glob": "test_*.py",
            "min_test_count": 1,
            "min_assertion_count": 1,
        })
        workspace = MagicMock()
        workspace.path = tmp_path
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_real.py").write_text("def test_x():\n    assert True\n")

        run = MagicMock()
        decision = await gate.validate(run, None, workspace)
        from horizonx.core.types import GateAction
        assert decision.decision == GateAction.CONTINUE

    def test_parse_score_from_pytest_output(self):
        gate = TestSuiteGate({"command": "pytest"})
        score = gate._parse_score("10 passed, 2 failed in 3.5s")
        assert abs(score - 10 / 12) < 0.001

    def test_parse_score_all_pass(self):
        gate = TestSuiteGate({"command": "pytest"})
        score = gate._parse_score("15 passed in 1.2s")
        assert score == 1.0
