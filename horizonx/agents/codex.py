"""Codex CLI driver — V1, validated against codex-cli 0.120+.

Wraps `codex exec --json`. Schema validated against
codex-rs/exec/src/exec_events.rs (ThreadEvent / ThreadItemDetails).

Top-level events:
  - {"type":"thread.started","thread_id":"<uuid>"}            # session id
  - {"type":"turn.started"}
  - {"type":"turn.completed","usage":{"input_tokens","cached_input_tokens","output_tokens"}}
  - {"type":"turn.failed","error":{"message":"..."}}
  - {"type":"item.started","item":{"id":"...","type":"...","..."}}
  - {"type":"item.updated","item":{...}}
  - {"type":"item.completed","item":{...}}
  - {"type":"error","message":"..."}

ThreadItemDetails (snake_case):
  - agent_message: {"text"}
  - reasoning: {"text"}
  - command_execution: {"command","aggregated_output","exit_code","status"}
  - file_change: {"changes":[{"path","kind":"add|delete|update"}],"status"}
  - todo_list: {"items":[...]}                # micro-plan
  - mcp_tool_call / collab_tool_call / web_search / error
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from horizonx.agents.base import CancelToken, Workspace, stream_subprocess_jsonl
from horizonx.core.types import (
    AgentConfig,
    SessionRunResult,
    SessionStatus,
    Step,
    StepType,
)


@dataclass
class CodexConfig:
    model: str = "gpt-5-codex"
    sandbox: Literal["read-only", "workspace-write", "danger-full-access"] | None = None
    full_auto: bool = True   # convenience for workspace-write sandbox
    skip_git_repo_check: bool = True
    ephemeral: bool = False  # don't persist session to ~/.codex
    add_dirs: list[str] = field(default_factory=list)
    binary: str = "codex"
    extra_args: list[str] = field(default_factory=list)
    # Legacy compat: reasoning_effort maps to -c model_reasoning_effort=...
    reasoning_effort: str | None = None
    output_schema_path: str | None = None  # JSON Schema file for structured response
    output_last_message_path: str | None = None
    config_overrides: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_agent_config(cls, ac: AgentConfig) -> "CodexConfig":
        overrides: dict[str, str] = {}
        if ac.reasoning_effort:
            # codex CLI exposes reasoning effort via TOML config override
            overrides["model_reasoning_effort"] = f'"{ac.reasoning_effort}"'
        overrides.update({k: str(v) for k, v in ac.extra.get("config_overrides", {}).items()})
        return cls(
            model=ac.model,
            sandbox=ac.extra.get("sandbox"),
            full_auto=bool(ac.extra.get("full_auto", True)),
            skip_git_repo_check=bool(ac.extra.get("skip_git_repo_check", True)),
            ephemeral=bool(ac.extra.get("ephemeral", False)),
            add_dirs=ac.extra.get("add_dirs", []),
            reasoning_effort=ac.reasoning_effort,
            output_schema_path=ac.extra.get("output_schema_path"),
            output_last_message_path=ac.extra.get("output_last_message_path"),
            config_overrides=overrides,
            extra_args=ac.extra.get("extra_args", []),
        )


class CodexAgent:
    """Validated Codex CLI driver."""

    name = "codex"

    def __init__(self, config: AgentConfig | CodexConfig):
        self.config = (
            config if isinstance(config, CodexConfig) else CodexConfig.from_agent_config(config)
        )
        self._totals: dict[str, int] = {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
        }

    @property
    def usage_totals(self) -> dict[str, int]:
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
        cmd = self._build_command(resume_session_id)
        captured_session_id = resume_session_id
        sequence = 0
        status = SessionStatus.COMPLETED
        last_error: str | None = None

        try:
            async for event in stream_subprocess_jsonl(
                cmd=cmd,
                cwd=workspace.path,
                stdin_data=session_prompt if not resume_session_id else None,
                env=workspace.env,
                cancel_token=cancel_token,
            ):
                self._accumulate_usage(event)
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
                error=f"codex binary not found: {exc}",
            )
        except Exception as exc:
            return SessionRunResult(
                agent_session_id=captured_session_id,
                status=SessionStatus.ERRORED,
                error=str(exc),
            )

        if cancel_token and cancel_token.cancelled:
            status = SessionStatus.SPIN if "spin" in cancel_token.reason else SessionStatus.TIMEOUT
        elif last_error:
            status = SessionStatus.ERRORED
        return SessionRunResult(
            agent_session_id=captured_session_id, status=status, error=last_error
        )

    # ---------------------------------------------------------------
    # Command construction
    # ---------------------------------------------------------------

    def _build_command(self, resume_session_id: str | None) -> list[str]:
        if resume_session_id:
            cmd = [self.config.binary, "exec", "resume", resume_session_id, "--json"]
        else:
            cmd = [self.config.binary, "exec", "--json"]

        if self.config.full_auto and not self.config.sandbox:
            cmd.append("--full-auto")
        elif self.config.sandbox:
            cmd += ["--sandbox", self.config.sandbox]

        if self.config.skip_git_repo_check:
            cmd.append("--skip-git-repo-check")
        if self.config.ephemeral:
            cmd.append("--ephemeral")

        cmd += ["-m", self.config.model]
        for d in self.config.add_dirs:
            cmd += ["--add-dir", d]
        if self.config.output_schema_path:
            cmd += ["--output-schema", self.config.output_schema_path]
        if self.config.output_last_message_path:
            cmd += ["-o", self.config.output_last_message_path]

        for key, val in self.config.config_overrides.items():
            cmd += ["-c", f"{key}={val}"]

        if self.config.extra_args:
            cmd += list(self.config.extra_args)

        if not resume_session_id:
            cmd.append("-")  # read prompt from stdin

        return cmd

    # ---------------------------------------------------------------
    # Event parsing — validated against codex-rs/exec/src/exec_events.rs
    # ---------------------------------------------------------------

    def _accumulate_usage(self, event: dict[str, Any]) -> None:
        if event.get("type") == "turn.completed":
            usage = event.get("usage") or {}
            for k in ("input_tokens", "cached_input_tokens", "output_tokens"):
                if usage.get(k) is not None:
                    self._totals[k] += int(usage[k])

    def _event_to_steps(self, event: dict[str, Any], sequence_start: int, session_id: str) -> list[Step]:
        et = event.get("type")
        seq = sequence_start
        out: list[Step] = []

        if et == "thread.started":
            out.append(Step(
                session_id=session_id, sequence=seq, type=StepType.SESSION_ID,
                content={"session_id": event.get("thread_id")},
            ))
            return out

        if et == "turn.started":
            out.append(Step(
                session_id=session_id, sequence=seq, type=StepType.SYSTEM,
                content={"event": "turn.started"},
            ))
            return out

        if et == "turn.completed":
            usage = event.get("usage") or {}
            out.append(Step(
                session_id=session_id, sequence=seq, type=StepType.USAGE,
                content={
                    "input_tokens": usage.get("input_tokens"),
                    "cached_input_tokens": usage.get("cached_input_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                },
            ))
            return out

        if et == "turn.failed":
            err = (event.get("error") or {}).get("message") or "turn.failed"
            out.append(Step(
                session_id=session_id, sequence=seq, type=StepType.ERROR,
                content={"error": err},
            ))
            return out

        if et in ("item.started", "item.updated", "item.completed"):
            return self._item_to_steps(event, sequence_start, session_id)

        if et == "error":
            out.append(Step(
                session_id=session_id, sequence=seq, type=StepType.ERROR,
                content={"error": event.get("message", "unknown error")},
            ))
            return out

        out.append(Step(
            session_id=session_id, sequence=seq, type=StepType.SYSTEM,
            content={"raw": event},
        ))
        return out

    def _item_to_steps(self, event: dict[str, Any], seq: int, session_id: str) -> list[Step]:
        item = event.get("item") or {}
        item_type = item.get("type")
        outer_event_type = event.get("type")  # item.started|updated|completed
        out: list[Step] = []

        # Skip 'started' for messages (they'll be repeated in completed); but emit for tools.
        if outer_event_type == "item.updated":
            return out  # interim — let completed event speak

        if item_type == "agent_message":
            if outer_event_type == "item.completed":
                out.append(Step(
                    session_id=session_id, sequence=seq, type=StepType.THOUGHT,
                    content={"text": item.get("text", ""), "item_id": item.get("id")},
                ))
        elif item_type == "reasoning":
            if outer_event_type == "item.completed":
                out.append(Step(
                    session_id=session_id, sequence=seq, type=StepType.REASONING,
                    content={"text": item.get("text", ""), "item_id": item.get("id")},
                ))
        elif item_type == "command_execution":
            # Emit on started AND completed (started = TOOL_CALL, completed = OBSERVATION)
            if outer_event_type == "item.started":
                out.append(Step(
                    session_id=session_id, sequence=seq, type=StepType.TOOL_CALL,
                    tool_name="command_execution",
                    content={"command": item.get("command"), "item_id": item.get("id")},
                ))
            elif outer_event_type == "item.completed":
                out.append(Step(
                    session_id=session_id, sequence=seq, type=StepType.OBSERVATION,
                    tool_name="command_execution",
                    content={
                        "command": item.get("command"),
                        "aggregated_output": item.get("aggregated_output"),
                        "exit_code": item.get("exit_code"),
                        "status": item.get("status"),
                        "item_id": item.get("id"),
                    },
                ))
        elif item_type == "file_change":
            if outer_event_type == "item.completed":
                out.append(Step(
                    session_id=session_id, sequence=seq, type=StepType.FILE_CHANGE,
                    tool_name="file_change",
                    content={
                        "changes": item.get("changes", []),
                        "status": item.get("status"),
                        "item_id": item.get("id"),
                    },
                ))
        elif item_type == "todo_list":
            # Emit on every state — micro-plan tracking
            out.append(Step(
                session_id=session_id, sequence=seq, type=StepType.TODO_LIST,
                tool_name="todo_list",
                content={
                    "items": item.get("items", []),
                    "phase": outer_event_type.split(".")[-1],
                    "item_id": item.get("id"),
                },
            ))
        elif item_type in ("mcp_tool_call", "collab_tool_call", "web_search"):
            if outer_event_type == "item.started":
                out.append(Step(
                    session_id=session_id, sequence=seq, type=StepType.TOOL_CALL,
                    tool_name=item_type,
                    content={k: v for k, v in item.items() if k != "id"} | {"item_id": item.get("id")},
                ))
            elif outer_event_type == "item.completed":
                out.append(Step(
                    session_id=session_id, sequence=seq, type=StepType.OBSERVATION,
                    tool_name=item_type,
                    content={k: v for k, v in item.items() if k != "id"} | {"item_id": item.get("id")},
                ))
        elif item_type == "error":
            out.append(Step(
                session_id=session_id, sequence=seq, type=StepType.ERROR,
                content={"error": item.get("message", "item error"), "item_id": item.get("id")},
            ))
        else:
            out.append(Step(
                session_id=session_id, sequence=seq, type=StepType.OBSERVATION,
                content={"raw_item": item, "phase": outer_event_type.split(".")[-1]},
            ))

        # Re-sequence
        for i, s in enumerate(out):
            s.sequence = seq + i
        return out
