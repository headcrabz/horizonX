"""SelfCritique strategy — iterating Implementer → Critic → Implementer loop.

Architecture:
  Round 0: Implementer session builds the first version.
  Round N: Critic session reviews the output, scores it, and writes critique.md.
           If score >= accept_threshold, converge and finish.
           Otherwise, Implementer session reads critique.md and improves.
  Max rounds: max_rounds (default: 5).

The Critic can be:
  - The same agent type (e.g., Claude Code reviewing its own code)
  - An LLM-as-judge call (cheaper, faster — critic_type: "llm")
  - A shell command (critic_type: "shell")

Filesystem handoff:
  critique.md   — Critic's structured feedback (score, issues, suggestions)
  progress.md   — cumulative history of rounds
  goals.json    — optional goal graph (inherited from task)

This strategy is novel for long-horizon code improvement loops (e.g., optimize
a function, refactor a module, harden a security-sensitive component).

See docs/LONG_HORIZON_AGENT.md §21.7.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from horizonx.agents.base import CancelToken, Workspace
from horizonx.agents.claude_code import ClaudeCodeAgent
from horizonx.agents.codex import CodexAgent
from horizonx.core.event_bus import Event
from horizonx.core.types import (
    AgentConfig,
    GateAction,
    Run,
    SessionStatus,
    Step,
    StepType,
)

CRITIQUE_SYSTEM = """\
You are a code critic for a self-improving agent loop. Your role is to:
1. Carefully review the implementation in the workspace
2. Score it on a 0.0-1.0 scale against the stated goal
3. Identify specific, actionable issues
4. Produce a structured critique that the next implementation session will read

Output ONLY a JSON object:
{
  "score": 0.0-1.0,
  "verdict": "accept" | "revise",
  "issues": [{"severity": "critical|major|minor", "description": "...", "location": "file:line"}],
  "suggestions": ["<concrete suggestion 1>", ...],
  "summary": "<2-3 sentence overall assessment>"
}

Be strict but fair. Score 0.85+ only if the implementation genuinely satisfies the goal.
"""

IMPLEMENTER_CONTINUATION_TEMPLATE = """\
This is implementation round {round_n} of {max_rounds}.

The previous round received the following critique:
---
{critique}
---

Overall score: {score:.2f} / 1.0 (threshold to accept: {threshold:.2f})

Address ALL issues marked as critical or major. The specific suggestions above are a guide.
After addressing them, the critic will re-evaluate.

Original task:
{original_prompt}
"""


def _build_agent(ac: AgentConfig) -> Any:
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
    raise ValueError(f"unknown agent type for SelfCritique: {ac.type}")


class SelfCritique:
    kind = "self_critique"

    def __init__(self, config: dict[str, Any]):
        self.max_rounds: int = config.get("max_rounds", 5)
        self.accept_threshold: float = config.get("accept_threshold", 0.85)
        self.critic_type: str = config.get("critic_type", "llm")  # llm | agent | shell
        self.critic_model: str = config.get("critic_model", "claude-haiku-4-5")
        self.critic_command: str | None = config.get("critic_command")  # for shell critic
        self.write_progress: bool = config.get("write_progress", True)

    async def execute(self, run: Run, rt: Any) -> AsyncIterator[Event]:
        workspace = Workspace(path=run.workspace_path, env={})
        agent = _build_agent(run.task.agent)
        history: list[dict[str, Any]] = []

        for round_n in range(self.max_rounds):
            # ---- Implementer session ----
            impl_session = await rt.start_session(run, target_goal=None)
            cancel = CancelToken()

            if round_n == 0:
                impl_prompt = run.task.prompt
            else:
                critique_path = run.workspace_path / "critique.md"
                critique_text = critique_path.read_text() if critique_path.exists() else ""
                last = history[-1] if history else {}
                impl_prompt = IMPLEMENTER_CONTINUATION_TEMPLATE.format(
                    round_n=round_n + 1,
                    max_rounds=self.max_rounds,
                    critique=critique_text[:3000],
                    score=last.get("score", 0.0),
                    threshold=self.accept_threshold,
                    original_prompt=run.task.prompt,
                )

            async def on_impl_step(step: Step, s=impl_session) -> None:
                step.session_id = s.id
                await rt.record_step(s, step)

            impl_result = await agent.run_session(
                impl_prompt, workspace,
                on_step=on_impl_step,
                cancel_token=cancel,
                session_id=impl_session.id,
            )
            if impl_result.agent_session_id:
                impl_session.agent_session_id = impl_result.agent_session_id
            await rt.end_session(impl_session, impl_result.status or SessionStatus.COMPLETED)

            if impl_result.status in {SessionStatus.ERRORED, SessionStatus.TIMEOUT}:
                yield Event(type="run.failed", run_id=run.id, payload={
                    "strategy": "self_critique",
                    "round": round_n,
                    "error": impl_result.error,
                })
                return

            # ---- Critic session ----
            critic_result = await self._run_critic(run, rt, workspace, round_n)
            score = critic_result.get("score", 0.0)
            verdict = critic_result.get("verdict", "revise")
            history.append({"round": round_n, "score": score, "verdict": verdict})

            # Write critique.md for next implementer session
            critique_md = self._format_critique(critic_result, round_n)
            (run.workspace_path / "critique.md").write_text(critique_md)

            # Append to progress.md
            if self.write_progress:
                self._append_progress(run.workspace_path, round_n, score, verdict)

            yield Event(type="step.recorded", run_id=run.id, payload={
                "strategy": "self_critique",
                "round": round_n,
                "score": score,
                "verdict": verdict,
            })

            if verdict == "accept" or score >= self.accept_threshold:
                # Run validators on final accepted state
                await rt.run_validators(run, None, when="final")
                yield Event(type="run.completed", run_id=run.id, payload={
                    "strategy": "self_critique",
                    "rounds": round_n + 1,
                    "final_score": score,
                })
                return

        # Max rounds exhausted — emit final score as-is
        final_score = history[-1]["score"] if history else 0.0
        await rt.run_validators(run, None, when="final")
        yield Event(type="run.completed", run_id=run.id, payload={
            "strategy": "self_critique",
            "rounds": self.max_rounds,
            "final_score": final_score,
            "note": "max_rounds reached without accepting",
        })

    async def _run_critic(
        self, run: Run, rt: Any, workspace: Workspace, round_n: int
    ) -> dict[str, Any]:
        if self.critic_type == "llm":
            return await self._llm_critic(run, workspace, round_n)
        if self.critic_type == "shell":
            return await self._shell_critic(workspace)
        if self.critic_type == "agent":
            return await self._agent_critic(run, rt, workspace, round_n)
        return {"score": 0.5, "verdict": "revise", "issues": [], "suggestions": [],
                "summary": f"unknown critic type: {self.critic_type}"}

    async def _llm_critic(self, run: Run, workspace: Workspace, round_n: int) -> dict[str, Any]:
        from horizonx.core.llm_client import call_llm_json

        # Collect workspace snapshot for critic
        files_context = self._collect_workspace_context(workspace.path)
        user_prompt = (
            f"Round {round_n + 1} critique.\n\n"
            f"GOAL:\n{run.task.prompt[:1000]}\n\n"
            f"WORKSPACE FILES:\n{files_context}"
        )
        try:
            result = await call_llm_json(
                system=CRITIQUE_SYSTEM,
                user_prompt=user_prompt,
                model=self.critic_model,
                max_tokens=2048,
                cache_system=True,
            )
            if "error" in result:
                return {"score": 0.5, "verdict": "revise", "issues": [],
                        "suggestions": [], "summary": "LLM critic failed"}
            return result
        except Exception as e:
            return {"score": 0.5, "verdict": "revise", "issues": [],
                    "suggestions": [str(e)], "summary": "LLM critic exception"}

    async def _shell_critic(self, workspace: Workspace) -> dict[str, Any]:
        import asyncio as aio
        if not self.critic_command:
            return {"score": 0.5, "verdict": "revise", "issues": [], "suggestions": [],
                    "summary": "no shell critic command configured"}
        proc = await aio.create_subprocess_shell(
            self.critic_command,
            cwd=str(workspace.path),
            stdout=aio.subprocess.PIPE,
            stderr=aio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        passed = proc.returncode == 0
        score = 1.0 if passed else 0.0
        return {
            "score": score,
            "verdict": "accept" if passed else "revise",
            "issues": [] if passed else [{"severity": "critical", "description": "shell critic failed",
                                          "location": "n/a"}],
            "suggestions": [],
            "summary": f"shell critic exit={proc.returncode}: {stdout.decode()[:200]}",
        }

    async def _agent_critic(
        self, run: Run, rt: Any, workspace: Workspace, round_n: int
    ) -> dict[str, Any]:
        agent = _build_agent(run.task.agent)
        critique_out = workspace.path / "_critic_output.json"
        critic_prompt = (
            f"You are a code critic. Review the workspace and output critique JSON to "
            f"`_critic_output.json`.\n\nCRITIQUE_SYSTEM:\n{CRITIQUE_SYSTEM}\n\n"
            f"GOAL:\n{run.task.prompt[:500]}"
        )
        critic_session = await rt.start_session(run, target_goal=None)

        async def on_critic_step(step: Step, s=critic_session) -> None:
            step.session_id = s.id
            await rt.record_step(s, step)

        await agent.run_session(
            critic_prompt, workspace,
            on_step=on_critic_step,
            session_id=critic_session.id,
        )
        await rt.end_session(critic_session, SessionStatus.COMPLETED)

        if critique_out.exists():
            try:
                return json.loads(critique_out.read_text())
            except json.JSONDecodeError:
                pass
        return {"score": 0.5, "verdict": "revise", "issues": [], "suggestions": [],
                "summary": "agent critic did not produce valid JSON"}

    def _collect_workspace_context(self, ws: Path, max_chars: int = 8000) -> str:
        lines: list[str] = []
        total = 0
        for p in sorted(ws.rglob("*")):
            if p.is_dir() or p.name.startswith("."):
                continue
            if p.suffix in (".pyc", ".db", ".lock", ".json") and p.name != "goals.json":
                continue
            try:
                content = p.read_text(errors="replace")[:2000]
            except OSError:
                continue
            chunk = f"\n### {p.relative_to(ws)}\n{content}"
            if total + len(chunk) > max_chars:
                lines.append(f"\n... (truncated at {max_chars} chars)")
                break
            lines.append(chunk)
            total += len(chunk)
        return "".join(lines) or "(empty workspace)"

    def _format_critique(self, result: dict[str, Any], round_n: int) -> str:
        issues = result.get("issues", [])
        suggestions = result.get("suggestions", [])
        return (
            f"# Critique — Round {round_n + 1}\n\n"
            f"**Score:** {result.get('score', 0):.2f} / 1.0\n"
            f"**Verdict:** {result.get('verdict', 'revise')}\n\n"
            f"## Summary\n{result.get('summary', '')}\n\n"
            f"## Issues\n" + "\n".join(
                f"- [{i.get('severity','?').upper()}] {i.get('description','')} "
                f"({i.get('location','')})"
                for i in issues
            ) + "\n\n"
            f"## Suggestions\n" + "\n".join(f"- {s}" for s in suggestions) + "\n"
        )

    def _append_progress(self, ws: Path, round_n: int, score: float, verdict: str) -> None:
        progress = ws / "progress.md"
        entry = f"\n## Round {round_n + 1} — score={score:.2f} verdict={verdict}\n"
        if progress.exists():
            progress.write_text(progress.read_text() + entry)
        else:
            progress.write_text(f"# SelfCritique progress\n{entry}")
