"""Identifies mandatory session cleanup steps that should not count against max_steps."""
from pathlib import Path

from horizonx.core.types import Step, StepType

HOUSEKEEPING_WRITE_TARGETS = frozenset({
    "summary.md", "progress.md", "decisions.jsonl",
    "failures.jsonl", "goals.json",
})

GIT_HOUSEKEEPING_PREFIXES = frozenset({
    "git add", "git commit",
})


def is_housekeeping_step(step: Step) -> bool:
    """Return True if this step is mandatory cleanup and should not consume the step budget."""
    if step.type != StepType.TOOL_CALL:
        return False
    tool = step.tool_name or ""
    content = step.content or {}

    if tool in ("Write", "Edit", "MultiEdit"):
        path = content.get("file_path") or content.get("path") or ""
        return Path(path).name in HOUSEKEEPING_WRITE_TARGETS

    if tool == "Bash":
        cmd = content.get("command") or ""
        return any(cmd.strip().startswith(p) for p in GIT_HOUSEKEEPING_PREFIXES)

    return False
