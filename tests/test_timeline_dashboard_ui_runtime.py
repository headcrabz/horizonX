"""Executable browser-independent tests for timeline dashboard state."""

from pathlib import Path
from subprocess import run


def test_timeline_dashboard_runtime_replays_durable_sse_summaries() -> None:
    script = Path(__file__).with_name("timeline_dashboard_ui_runtime.test.js")
    result = run(["node", str(script)], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr or result.stdout
