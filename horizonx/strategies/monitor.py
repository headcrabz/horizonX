"""MonitorRespond — event-driven reactive strategy.

Polls a trigger condition on a configurable cadence. When the trigger fires,
runs one responder session. Resets and polls again.

Use cases:
  - Alert response: watch a metric, patch when threshold exceeded
  - Continuous ingestion: pull new data, process, repeat
  - Scheduled reporting: generate report when cron trigger fires

Architecture:
  Loop:
    1. Poll trigger (shell command or metric threshold)
    2. If triggered: run one agent session (the "responder")
    3. Run validators; handle HITL if needed
    4. Sleep poll_interval_seconds
    5. Exit when max_triggers reached or run time budget exhausted

See docs/LONG_HORIZON_AGENT.md §21.6.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator
from typing import Any

from horizonx.agents.base import CancelToken, Workspace
from horizonx.core.event_bus import Event
from horizonx.core.types import Run, RunStatus, SessionStatus, Step, StrategyOutcome
from horizonx.strategies._agent_builder import build_agent as _build_agent


class MonitorRespond:
    kind = "monitor"

    def __init__(self, config: dict[str, Any]):
        self.trigger_command: str | None = config.get("trigger_command")
        self.trigger_metric_command: str | None = config.get("trigger_metric_command")
        self.trigger_threshold: float = config.get("trigger_threshold", 1.0)
        self.trigger_direction: str = config.get("trigger_direction", "ge")  # ge|le|eq
        self.poll_interval_seconds: float = config.get("poll_interval_seconds", 30.0)
        self.max_triggers: int | None = config.get("max_triggers")
        self.responder_prompt_template: str = config.get(
            "responder_prompt_template",
            "The monitoring trigger fired. Take appropriate action.\n\n{base_prompt}"
        )

    async def execute(self, run: Run, rt: Any) -> AsyncIterator[Event | StrategyOutcome]:
        yield Event(type="run.started", run_id=run.id, payload={
            "strategy": "monitor",
            "poll_interval_seconds": self.poll_interval_seconds,
        })

        triggers_fired = 0
        start = time.monotonic()
        time_budget_exhausted = False

        while True:
            # Check resource budget
            elapsed_hours = (time.monotonic() - start) / 3600
            if run.task.resources.max_total_hours is not None and elapsed_hours >= run.task.resources.max_total_hours:
                time_budget_exhausted = True
                break
            if self.max_triggers is not None and triggers_fired >= self.max_triggers:
                break

            triggered = await self._check_trigger(run.workspace_path)
            if triggered:
                triggers_fired += 1
                yield Event(type="goal.in_progress", run_id=run.id, payload={
                    "trigger_count": triggers_fired, "elapsed_hours": round(elapsed_hours, 2),
                })
                responder_outcome = await self._run_responder(run, rt, triggers_fired)
                if responder_outcome is not None:
                    yield responder_outcome
                    return
                await rt.run_validators(run, None, when="after_every_session")

            await asyncio.sleep(self.poll_interval_seconds)

        yield StrategyOutcome(
            status=(
                RunStatus.TIMED_OUT if time_budget_exhausted else RunStatus.COMPLETED
            ),
            reason="monitor_time_budget_exhausted" if time_budget_exhausted else None,
            details={"triggers_fired": triggers_fired},
        )

    async def _check_trigger(self, workspace_path: Any) -> bool:
        if self.trigger_command:
            proc = await asyncio.create_subprocess_shell(
                self.trigger_command,
                cwd=str(workspace_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            return proc.returncode == 0

        if self.trigger_metric_command:
            proc = await asyncio.create_subprocess_shell(
                self.trigger_metric_command,
                cwd=str(workspace_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            text = stdout.decode().strip()
            nums = re.findall(r"[-+]?\d*\.?\d+", text)
            if not nums:
                return False
            try:
                value = float(nums[-1])
            except ValueError:
                return False
            return self._threshold_met(value)

        return False

    def _threshold_met(self, value: float) -> bool:
        if self.trigger_direction == "ge":
            return value >= self.trigger_threshold
        if self.trigger_direction == "le":
            return value <= self.trigger_threshold
        return abs(value - self.trigger_threshold) < 1e-9

    async def _run_responder(
        self, run: Run, rt: Any, trigger_count: int
    ) -> StrategyOutcome | None:
        session = await rt.start_session(run, target_goal=None)
        agent = _build_agent(run.task.agent)
        workspace = Workspace(path=run.workspace_path, env={})
        cancel = CancelToken()

        prompt = self.responder_prompt_template.format(
            base_prompt=run.task.prompt,
            trigger_count=trigger_count,
        )

        async def on_step(step: Step, s: Any = session) -> None:
            step.session_id = s.id
            await rt.record_step(s, step)

        result = await agent.run_session(
            prompt, workspace, on_step=on_step,
            cancel_token=cancel, session_id=session.id,
        )
        if result.agent_session_id:
            session.agent_session_id = result.agent_session_id
        rt.charge(result)
        await rt.end_session(session, result.status or SessionStatus.COMPLETED)
        if result.status != SessionStatus.COMPLETED:
            return StrategyOutcome(
                status=(
                    RunStatus.TIMED_OUT
                    if result.status == SessionStatus.TIMEOUT
                    else RunStatus.FAILED
                ),
                reason=f"responder_{result.status.value}",
                details={"trigger_count": trigger_count, "error": result.error},
            )
        return None
