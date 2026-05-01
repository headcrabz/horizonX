"""Claude Code CLI driver — V1, validated against `claude` v2.1+.

Wraps `claude --print --output-format stream-json --verbose --bare`.
Schema validated against actual stream output (claude-cli v2.1.123).

Top-level events:
  - {"type":"system","subtype":"init","session_id":..., "model":..., "tools":..., "uuid":...}
  - {"type":"assistant","message":{"id","content":[<blocks>], "usage":{...}},"session_id":...}
  - {"type":"result","subtype":"success","is_error":bool,"duration_ms":..., "total_cost_usd":..., "usage":{...}}
  - {"type":"user","message":{"content":[<tool_result blocks>]},"session_id":...}     # tool results
  - {"type":"hook_event", ...}                                                          # if --include-hook-events

Content blocks inside assistant.message.content:
  - {"type":"text","text":"..."}
  - {"type":"thinking","thinking":"..."}                          # extended thinking
  - {"type":"tool_use","id":"toolu_...","name":"Bash","input":{...}}
Inside user.message.content:
  - {"type":"tool_result","tool_use_id":"toolu_...","content":[...]|"...", "is_error":bool}

Prompt caching: usage carries cache_creation_input_tokens, cache_read_input_tokens.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from horizonx.agents.base import CancelToken, Workspace, stream_subprocess_jsonl
from horizonx.core.types import (
    AgentConfig,
    SessionRunResult,
    SessionStatus,
    Step,
    StepType,
)


@dataclass
class ClaudeCodeConfig:
    model: str = "claude-opus-4-7"
    allowed_tools: list[str] | None = None
    disallowed_tools: list[str] | None = None
    effort: str | None = None  # low|medium|high|xhigh|max — Claude Code's reasoning effort
    mcp_config_path: str | None = None
    bare: bool = True
    binary: str = "claude"
    additional_dirs: list[str] = field(default_factory=list)
    system_prompt: str | None = None  # replaces default system prompt
    append_system_prompt: str | None = None  # appended to default
    max_budget_usd: float | None = None  # native --max-budget-usd
    permission_mode: str | None = None  # acceptEdits|auto|bypassPermissions|default|dontAsk|plan
    extra_args: list[str] = field(default_factory=list)
    use_session_id: bool = True   # let HorizonX assign UUID for the session
    no_session_persistence: bool = False  # if True, sessions can't be resumed

    @classmethod
    def from_agent_config(cls, ac: AgentConfig) -> "ClaudeCodeConfig":
        return cls(
            model=ac.model,
            allowed_tools=ac.allowed_tools,
            effort=ac.extra.get("effort") or _thinking_to_effort(ac.thinking_budget),
            mcp_config_path=ac.mcp_config_path,
            additional_dirs=ac.extra.get("additional_dirs", []),
            system_prompt=ac.extra.get("system_prompt"),
            append_system_prompt=ac.extra.get("append_system_prompt"),
            max_budget_usd=ac.extra.get("max_budget_usd"),
            permission_mode=ac.extra.get("permission_mode", "bypassPermissions"),
            extra_args=ac.extra.get("extra_args", []),
        )


def _thinking_to_effort(thinking_budget: int | None) -> str | None:
    """Translate legacy thinking_budget int → Claude Code's --effort level."""
    if thinking_budget is None:
        return None
    if thinking_budget <= 2000:
        return "low"
    if thinking_budget <= 6000:
        return "medium"
    if thinking_budget <= 16000:
        return "high"
    return "xhigh"


class ClaudeCodeAgent:
    """Validated Claude Code CLI driver.

    Honors prompt caching (visible in usage events), captures TodoWrite events
    as TODO_LIST step type, and aggregates token / cost metrics from result event.
    """

    name = "claude-code"

    def __init__(self, config: AgentConfig | ClaudeCodeConfig):
        self.config = (
            config
            if isinstance(config, ClaudeCodeConfig)
            else ClaudeCodeConfig.from_agent_config(config)
        )
        self._totals: dict[str, float] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "total_cost_usd": 0.0,
        }

    @property
    def usage_totals(self) -> dict[str, float]:
        return dict(self._totals)

    async def run_session(
        self,
        session_prompt: str,
        workspace: Workspace,
        *,
        resume_session_id: str | None = None,
        on_step: Callable[[Step], Awaitable[None]] | None = None,
        cancel_token: CancelToken | None = None,
        session_id: str | None = None,
    ) -> SessionRunResult:
        # If we want a deterministic session id (for resume), generate one and pass via --session-id
        new_session_uuid = str(uuid4()) if self.config.use_session_id and not resume_session_id else None
        cmd = self._build_command(resume_session_id, new_session_uuid)
        captured_session_id = resume_session_id or new_session_uuid
        sequence = 0
        status = SessionStatus.COMPLETED
        last_error: str | None = None

        try:
            async for event in stream_subprocess_jsonl(
                cmd=cmd,
                cwd=workspace.path,
                stdin_data=session_prompt,
                env=workspace.env,
                cancel_token=cancel_token,
            ):
                # Track usage from any event that carries it
                self._accumulate_usage(event)
                # Translate to one or more Steps
                for step in self._event_to_steps(event, sequence_start=sequence, session_id=session_id or ""):
                    sequence = step.sequence + 1
                    if step.type == StepType.SESSION_ID:
                        captured_session_id = step.content.get("session_id") or captured_session_id
                    if step.type == StepType.ERROR:
                        last_error = step.content.get("error") or last_error
                    if on_step:
                        await on_step(step)
        except FileNotFoundError as exc:
            return SessionRunResult(
                agent_session_id=captured_session_id,
                status=SessionStatus.ERRORED,
                error=f"claude binary not found: {exc}",
            )
        except Exception as exc:
            return SessionRunResult(
                agent_session_id=captured_session_id,
                status=SessionStatus.ERRORED,
                error=str(exc),
            )

        if cancel_token and cancel_token.cancelled:
            status = (
                SessionStatus.SPIN if "spin" in cancel_token.reason else SessionStatus.TIMEOUT
            )
        elif last_error:
            status = SessionStatus.ERRORED
        return SessionRunResult(
            agent_session_id=captured_session_id, status=status, error=last_error
        )

    # ---------------------------------------------------------------
    # Command construction
    # ---------------------------------------------------------------

    def _build_command(self, resume_session_id: str | None, new_session_uuid: str | None) -> list[str]:
        cmd = [
            self.config.binary,
            "--print",
            "--output-format", "stream-json",
            "--verbose",  # required for stream-json + --print
            "--input-format", "text",
            "--model", self.config.model,
        ]
        if self.config.bare:
            cmd.append("--bare")
        if self.config.no_session_persistence:
            cmd.append("--no-session-persistence")
        if resume_session_id:
            cmd += ["--resume", resume_session_id]
        elif new_session_uuid:
            cmd += ["--session-id", new_session_uuid]
        if self.config.allowed_tools:
            cmd += ["--allowed-tools", ",".join(self.config.allowed_tools)]
        if self.config.disallowed_tools:
            cmd += ["--disallowed-tools", ",".join(self.config.disallowed_tools)]
        if self.config.effort:
            cmd += ["--effort", self.config.effort]
        if self.config.mcp_config_path:
            cmd += ["--mcp-config", self.config.mcp_config_path]
        if self.config.system_prompt:
            cmd += ["--system-prompt", self.config.system_prompt]
        if self.config.append_system_prompt:
            cmd += ["--append-system-prompt", self.config.append_system_prompt]
        if self.config.max_budget_usd is not None:
            cmd += ["--max-budget-usd", str(self.config.max_budget_usd)]
        if self.config.permission_mode:
            cmd += ["--permission-mode", self.config.permission_mode]
        for d in self.config.additional_dirs:
            cmd += ["--add-dir", d]
        if self.config.extra_args:
            cmd += list(self.config.extra_args)
        return cmd

    # ---------------------------------------------------------------
    # Event parsing — validated against claude v2.1.123 stream-json
    # ---------------------------------------------------------------

    def _accumulate_usage(self, event: dict[str, Any]) -> None:
        # 'result' event has top-level usage; 'assistant' event nests usage in message.usage
        usage = None
        if event.get("type") == "result":
            usage = event.get("usage")
            if event.get("total_cost_usd") is not None:
                self._totals["total_cost_usd"] += float(event["total_cost_usd"])
        elif event.get("type") == "assistant":
            usage = (event.get("message") or {}).get("usage")
        if not usage:
            return
        for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
            if usage.get(k):
                self._totals[k] += int(usage[k])

    def _event_to_steps(self, event: dict[str, Any], sequence_start: int, session_id: str) -> list[Step]:
        """Translate one Claude stream event into one or more Steps."""
        et = event.get("type")
        seq = sequence_start
        out: list[Step] = []

        if et == "system" and event.get("subtype") == "init":
            out.append(Step(
                session_id=session_id, sequence=seq, type=StepType.SYSTEM,
                content={
                    "subtype": "init",
                    "session_id": event.get("session_id"),
                    "model": event.get("model"),
                    "tools": event.get("tools"),
                    "version": event.get("claude_code_version"),
                    "permission_mode": event.get("permissionMode"),
                },
            ))
            seq += 1
            if event.get("session_id"):
                out.append(Step(
                    session_id=session_id, sequence=seq, type=StepType.SESSION_ID,
                    content={"session_id": event["session_id"]},
                ))
            return out

        if et == "assistant":
            msg = event.get("message") or {}
            for block in msg.get("content") or []:
                btype = block.get("type")
                if btype == "text":
                    out.append(Step(
                        session_id=session_id, sequence=seq, type=StepType.THOUGHT,
                        content={"text": block.get("text", "")},
                    ))
                    seq += 1
                elif btype == "thinking":
                    out.append(Step(
                        session_id=session_id, sequence=seq, type=StepType.REASONING,
                        content={"text": block.get("thinking", "")},
                    ))
                    seq += 1
                elif btype == "tool_use":
                    name = block.get("name", "")
                    inp = block.get("input") or {}
                    # Capture TodoWrite as a TODO_LIST step (short-goal tracking)
                    if name == "TodoWrite":
                        out.append(Step(
                            session_id=session_id, sequence=seq, type=StepType.TODO_LIST,
                            tool_name=name,
                            content={"todos": inp.get("todos", []), "tool_use_id": block.get("id")},
                        ))
                    else:
                        out.append(Step(
                            session_id=session_id, sequence=seq, type=StepType.TOOL_CALL,
                            tool_name=name,
                            content={"input": inp, "tool_use_id": block.get("id")},
                        ))
                    seq += 1
                else:
                    # Unknown block — record as observation-with-raw
                    out.append(Step(
                        session_id=session_id, sequence=seq, type=StepType.OBSERVATION,
                        content={"raw_block": block},
                    ))
                    seq += 1
            return out

        if et == "user":
            # User-role messages in the stream contain tool_result blocks
            msg = event.get("message") or {}
            for block in msg.get("content") or []:
                if block.get("type") == "tool_result":
                    out.append(Step(
                        session_id=session_id, sequence=seq, type=StepType.OBSERVATION,
                        content={
                            "tool_use_id": block.get("tool_use_id"),
                            "output": block.get("content"),
                            "is_error": block.get("is_error", False),
                        },
                    ))
                    seq += 1
            return out

        if et == "result":
            out.append(Step(
                session_id=session_id, sequence=seq, type=StepType.USAGE,
                content={
                    "subtype": event.get("subtype"),
                    "is_error": event.get("is_error", False),
                    "duration_ms": event.get("duration_ms"),
                    "duration_api_ms": event.get("duration_api_ms"),
                    "num_turns": event.get("num_turns"),
                    "total_cost_usd": event.get("total_cost_usd"),
                    "usage": event.get("usage"),
                    "result": event.get("result"),
                    "stop_reason": event.get("stop_reason"),
                    "terminal_reason": event.get("terminal_reason"),
                },
            ))
            seq += 1
            if event.get("is_error"):
                out.append(Step(
                    session_id=session_id, sequence=seq, type=StepType.ERROR,
                    content={"error": event.get("result", "unspecified error")},
                ))
            return out

        if et == "error":
            out.append(Step(
                session_id=session_id, sequence=seq, type=StepType.ERROR,
                content={"error": event.get("message", "unknown error"), "raw": event},
            ))
            return out

        # Hook event or other — bucket as system
        out.append(Step(
            session_id=session_id, sequence=seq, type=StepType.SYSTEM,
            content={"raw": event},
        ))
        return out
