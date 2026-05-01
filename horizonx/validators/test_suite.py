"""TestSuiteGate — runs pytest/jest/cargo with layered anti-gaming guards.

Anti-gaming layers (all must pass):
  1. Test file count >= min_test_count (files can't silently disappear)
  2. Assertion count >= min_assertion_count (tests can't be emptied)
  3. Execution time >= min_exec_seconds (suspiciously fast = mocked/empty)
  4. Exit code == 0 (tests actually pass)

Each guard fires independently; the worst outcome wins.
See docs/LONG_HORIZON_AGENT.md §25.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any

from horizonx.core.types import GateAction, GateDecision, Run, Session


_ASSERT_PATTERNS = [
    # Python pytest: assert X, assert_* calls
    re.compile(r"^\s*assert\b", re.MULTILINE),
    re.compile(r"\bassert(?:Raises|Equal|True|False|In|Is|Greater|Less)\b"),
    # Jest/Mocha: expect(...).toXxx()
    re.compile(r"\bexpect\s*\("),
    # Rust: assert!, assert_eq!, assert_ne!
    re.compile(r"\bassert(?:_eq|_ne)?!\s*\("),
    # Go: t.Error, t.Fatal, t.Assert (testify)
    re.compile(r"\bt\.(?:Error|Fatal|Errorf|Fatalf|Assert|Require)\b"),
]


def _count_assertions(test_dir: Path, glob: str) -> int:
    total = 0
    for f in test_dir.rglob("*"):
        if not f.is_file():
            continue
        from fnmatch import fnmatch
        if not fnmatch(f.name, glob):
            continue
        try:
            src = f.read_text(errors="replace")
        except OSError:
            continue
        for pat in _ASSERT_PATTERNS:
            total += len(pat.findall(src))
    return total


class TestSuiteGate:
    name = "test_suite"

    def __init__(self, config: dict[str, Any]):
        self.command: str = config["command"]
        self.test_dir: str = config.get("test_dir", "tests/")
        self.test_glob: str = config.get("test_glob", "test_*.py")
        self.min_test_count: int = config.get("min_test_count", 0)
        self.min_assertion_count: int = config.get("min_assertion_count", 0)
        self.min_exec_seconds: float = config.get("min_exec_seconds", 0.0)
        self.timeout_seconds: float = config.get("timeout_seconds", 300.0)
        self.on_fail: str = config.get("on_fail", "pause_for_hitl")
        self.runs: str = config.get("runs", "after_every_session")
        self._id: str = config.get("id", "test_suite")

    async def validate(self, run: Run, session: Session | None, workspace: Any) -> GateDecision:
        start = time.monotonic()
        td = workspace.path / self.test_dir

        test_count = self._count_tests(td)
        assertion_count = _count_assertions(td, self.test_glob) if td.exists() else 0

        # Guard 1: file count
        if self.min_test_count and test_count < self.min_test_count:
            return GateDecision(
                decision=GateAction(self.on_fail),
                reason=(
                    f"test file count {test_count} < min {self.min_test_count} "
                    "— agent may have deleted test files"
                ),
                score=0.0,
                details={"test_count": test_count, "min_test_count": self.min_test_count},
                suggested_modification="Restore deleted test files. Never delete tests to make the suite pass.",
                validator_name=self._id,
                duration_ms=int((time.monotonic() - start) * 1000),
            )

        # Guard 2: assertion count
        if self.min_assertion_count and assertion_count < self.min_assertion_count:
            return GateDecision(
                decision=GateAction(self.on_fail),
                reason=(
                    f"assertion count {assertion_count} < min {self.min_assertion_count} "
                    "— test bodies may have been emptied"
                ),
                score=0.0,
                details={
                    "assertion_count": assertion_count,
                    "min_assertion_count": self.min_assertion_count,
                    "test_count": test_count,
                },
                suggested_modification="Tests must contain real assertions. `pass` or empty bodies are not acceptable.",
                validator_name=self._id,
                duration_ms=int((time.monotonic() - start) * 1000),
            )

        # Run the suite
        exec_start = time.monotonic()
        proc = await asyncio.create_subprocess_shell(
            self.command,
            cwd=str(workspace.path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            proc.kill()
            return GateDecision(
                decision=GateAction(self.on_fail),
                reason=f"test suite timed out after {self.timeout_seconds}s",
                score=0.0,
                details={"command": self.command},
                validator_name=self._id,
                duration_ms=int((time.monotonic() - start) * 1000),
            )

        exec_seconds = time.monotonic() - exec_start

        # Guard 3: suspiciously fast execution
        if self.min_exec_seconds and exec_seconds < self.min_exec_seconds:
            return GateDecision(
                decision=GateAction(self.on_fail),
                reason=(
                    f"test suite completed in {exec_seconds:.2f}s < min {self.min_exec_seconds}s "
                    "— tests may be mocked/empty or skipping real work"
                ),
                score=0.0,
                details={
                    "exec_seconds": round(exec_seconds, 3),
                    "min_exec_seconds": self.min_exec_seconds,
                    "returncode": proc.returncode,
                },
                suggested_modification="Tests must exercise real code paths, not be trivially short.",
                validator_name=self._id,
                duration_ms=int((time.monotonic() - start) * 1000),
            )

        # Guard 4: exit code
        passed = proc.returncode == 0
        action = GateAction.CONTINUE if passed else GateAction(self.on_fail)
        stdout_text = (stdout or b"").decode(errors="replace")

        # Parse pytest summary line for finer score
        score = self._parse_score(stdout_text) if passed else 0.0

        return GateDecision(
            decision=action,
            reason=(
                f"tests {'passed' if passed else 'failed'} "
                f"(files={test_count}, assertions={assertion_count}, "
                f"exec={exec_seconds:.1f}s)"
            ),
            score=score,
            details={
                "test_count": test_count,
                "assertion_count": assertion_count,
                "exec_seconds": round(exec_seconds, 3),
                "returncode": proc.returncode,
                "stdout_tail": stdout_text[-3000:],
                "stderr_tail": (stderr or b"").decode(errors="replace")[-1000:],
            },
            validator_name=self._id,
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    def _count_tests(self, test_dir: Path) -> int:
        from fnmatch import fnmatch
        if not test_dir.exists():
            return 0
        return sum(1 for p in test_dir.rglob("*") if p.is_file() and fnmatch(p.name, self.test_glob))

    def _parse_score(self, stdout: str) -> float:
        """Extract pass rate from pytest summary line: '5 passed, 1 failed'."""
        m = re.search(r"(\d+) passed", stdout)
        f = re.search(r"(\d+) failed", stdout)
        passed_n = int(m.group(1)) if m else 0
        failed_n = int(f.group(1)) if f else 0
        total = passed_n + failed_n
        if total == 0:
            return 1.0
        return passed_n / total
