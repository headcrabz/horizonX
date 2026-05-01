"""Summarizer — structured inter-session handoff via LLM.

Produces the filesystem handoff files (summary.md) that bridge sessions.
This is distinct from the agent's built-in context compaction (which handles
within-session context). The Summarizer creates structured data for the NEXT
session, which starts with zero context.

System prompt is cached via Anthropic's prompt caching (5-min TTL) so repeated
summarize calls within a run reuse the cached prefix.

See docs/LONG_HORIZON_AGENT.md §15.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from horizonx.core.types import Run, Session, SessionSummary, SummarizerConfig, Step, StepType

logger = logging.getLogger(__name__)

SUMMARIZER_SYSTEM = """\
You are a session summarizer for a long-horizon agent execution framework.
Your job is to produce a structured handoff document so the NEXT agent session
can pick up exactly where this one left off — with zero prior context.

Output ONLY a JSON object with these fields:
{
  "summary_md": "<1-page narrative, <1500 words. Cite file paths and line numbers.>",
  "key_decisions": ["<decision 1>", ...],
  "blockers": ["<blocker 1>", ...],
  "next_actions": ["<specific action 1>", ...],
  "files_modified": ["path/a.py", ...],
  "tests_status": {"passing": N, "failing": N, "added": N, "deleted": N},
  "confidence": 0.0-1.0
}

Rules:
- summary_md must be specific enough for a new agent to continue without re-reading files
- key_decisions: architectural choices, trade-offs, rejected alternatives
- blockers: anything that prevented completion (errors, missing deps, unclear spec)
- next_actions: concrete, actionable items — not vague ("implement feature X" not "continue work")
- files_modified: every file touched, even if reverted
- confidence: your assessment of how much of the goal was achieved (0=nothing, 1=fully done)
"""


class Summarizer:
    """Produces structured handoff summary for inter-session continuity.

    NOT a replacement for the agent's internal context compaction —
    this creates the filesystem handoff that bridges session boundaries.
    """

    def __init__(self, config: SummarizerConfig, store: Any):
        self.config = config
        self.store = store

    async def summarize(self, session: Session, run: Run) -> Path:
        if not self.config.enabled:
            return Path()

        steps = await self.store.recent_steps(session.id, 1000)
        goal = await self._goal_for_session(run, session)
        trajectory_text = self._compress_steps(steps)

        goal_name = goal.name if goal else session.target_goal_id or "(no specific goal)"
        goal_desc = goal.description if goal else ""

        user_prompt = (
            f"Session just completed for:\n"
            f"  Goal: {goal_name}\n"
            f"  Description: {goal_desc}\n\n"
            f"TRAJECTORY ({len(steps)} steps):\n{trajectory_text}"
        )

        summary_data = await self._call_llm(user_prompt)
        if "error" in summary_data:
            logger.warning("Summarizer LLM call failed: %s", summary_data.get("error"))
            summary_data = self._fallback_summary(steps, goal_name)

        summary = SessionSummary(
            session_id=session.id,
            target_goal_id=session.target_goal_id,
            summary_md=summary_data.get("summary_md", ""),
            key_decisions=summary_data.get("key_decisions", []),
            blockers=summary_data.get("blockers", []),
            next_actions=summary_data.get("next_actions", []),
            files_modified=summary_data.get("files_modified", []),
            tests_status=summary_data.get("tests_status", {}),
            confidence=float(summary_data.get("confidence", 0.5)),
        )

        summary_path = run.workspace_path / "summary.md"
        summary_path.write_text(self._format_summary_md(summary))

        goals_path = run.workspace_path / "goals.json"
        if goals_path.exists():
            try:
                goals = json.loads(goals_path.read_text())
                if isinstance(goals, dict) and session.target_goal_id:
                    goals[session.target_goal_id] = {
                        "confidence": summary.confidence,
                        "blockers": summary.blockers,
                        "next_actions": summary.next_actions,
                    }
                    goals_path.write_text(json.dumps(goals, indent=2))
            except (json.JSONDecodeError, OSError):
                pass

        return summary_path

    def _compress_steps(self, steps: list[Step]) -> str:
        lines: list[str] = []
        for s in steps[-200:]:
            label = s.tool_name or s.type.value
            if s.type == StepType.THOUGHT:
                text = s.content.get("text", "")[:300]
                lines.append(f"[{s.sequence}] THOUGHT: {text}")
            elif s.type == StepType.TOOL_CALL:
                content_preview = json.dumps(s.content, default=str)[:250]
                lines.append(f"[{s.sequence}] CALL {label}: {content_preview}")
            elif s.type == StepType.OBSERVATION:
                output = s.content.get("output") or s.content.get("aggregated_output") or ""
                if isinstance(output, str):
                    output = output[:200]
                else:
                    output = json.dumps(output, default=str)[:200]
                is_err = s.content.get("is_error", False)
                prefix = "ERR" if is_err else "OBS"
                lines.append(f"[{s.sequence}] {prefix} {label}: {output}")
            elif s.type == StepType.FILE_CHANGE:
                changes = s.content.get("changes", [])
                desc = ", ".join(f"{c.get('kind','?')} {c.get('path','?')}" for c in changes[:5])
                lines.append(f"[{s.sequence}] FILE_CHANGE: {desc}")
            elif s.type == StepType.ERROR:
                lines.append(f"[{s.sequence}] ERROR: {s.content.get('error', '')[:200]}")
            elif s.type in (StepType.USAGE, StepType.SESSION_ID, StepType.SYSTEM):
                continue
            else:
                content_preview = json.dumps(s.content, default=str)[:150]
                lines.append(f"[{s.sequence}] {label}: {content_preview}")
        return "\n".join(lines)

    def _fallback_summary(self, steps: list[Step], goal_name: str) -> dict[str, Any]:
        files = set()
        errors: list[str] = []
        for s in steps:
            if s.type == StepType.FILE_CHANGE:
                for c in s.content.get("changes", []):
                    if c.get("path"):
                        files.add(c["path"])
            if s.type == StepType.ERROR:
                errors.append(s.content.get("error", "")[:100])
        return {
            "summary_md": f"Session on '{goal_name}' completed with {len(steps)} steps. "
            f"Modified {len(files)} files. {len(errors)} errors encountered. "
            "LLM summarization was unavailable; this is a fallback summary.",
            "key_decisions": [],
            "blockers": errors[:5],
            "next_actions": ["Review session trajectory manually"],
            "files_modified": sorted(files),
            "tests_status": {},
            "confidence": 0.3,
        }

    async def _goal_for_session(self, run: Run, session: Session) -> Any:
        if not session.target_goal_id:
            return None
        return await self.store.load_goal(run.id, session.target_goal_id)

    async def _call_llm(self, user_prompt: str) -> dict[str, Any]:
        from horizonx.core.llm_client import call_llm_json

        return await call_llm_json(
            system=SUMMARIZER_SYSTEM,
            user_prompt=user_prompt,
            model=self.config.model,
            max_tokens=self.config.max_tokens_per_summary,
            cache_system=True,
        )

    def _format_summary_md(self, s: SessionSummary) -> str:
        sections = [
            f"# Session summary — {s.session_id}",
            f"\n**Target goal:** {s.target_goal_id}",
            f"**Confidence:** {s.confidence}",
            f"\n## Narrative\n{s.summary_md}",
        ]
        if s.key_decisions:
            sections.append("\n## Key decisions\n" + "\n".join(f"- {d}" for d in s.key_decisions))
        if s.blockers:
            sections.append("\n## Blockers\n" + "\n".join(f"- {b}" for b in s.blockers))
        if s.next_actions:
            sections.append("\n## Next actions\n" + "\n".join(f"- {a}" for a in s.next_actions))
        if s.files_modified:
            sections.append(
                "\n## Files modified\n" + "\n".join(f"- `{f}`" for f in s.files_modified)
            )
        if s.tests_status:
            sections.append(f"\n## Tests status\n```json\n{json.dumps(s.tests_status, indent=2)}\n```")
        return "\n".join(sections) + "\n"
