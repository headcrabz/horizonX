"""One shared lifecycle for bounded agent attempts."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from horizonx.agents.base import CancelToken, Workspace
from horizonx.agents.registry import build_agent
from horizonx.core.attempt_result import AttemptResult
from horizonx.core.governor import BudgetExceeded
from horizonx.core.types import (
    AgentConfig,
    GateDecision,
    GoalNode,
    Run,
    SessionRunResult,
    SessionStatus,
    Step,
    StepType,
)

if TYPE_CHECKING:
    from horizonx.core.runtime import Runtime

CleanupCallback = Callable[[], Awaitable[None]]


class AttemptExecutor:
    """Own agent construction, session state, recording, policy checks, and cleanup."""

    def __init__(self, runtime: Runtime):
        self.runtime = runtime

    async def execute(
        self,
        run: Run,
        *,
        prompt: str,
        target_goal: GoalNode | None = None,
        agent_config: AgentConfig | None = None,
        workspace_path: Path | None = None,
        session_id: str | None = None,
        resume_session_id: str | None = None,
        validator_stages: tuple[str, ...] = (),
        timeout_seconds: float | None = None,
        cleanup: tuple[CleanupCallback, ...] = (),
    ) -> AttemptResult:
        rt = self.runtime
        session = await rt.start_session(
            run, target_goal=target_goal, session_id=session_id
        )
        cancel_token = CancelToken()
        decisions: list[GateDecision] = []
        spin_detected = False
        result: SessionRunResult | None = None

        async def on_step(step: Step) -> None:
            nonlocal spin_detected
            step.session_id = session.id
            if step.type == StepType.SESSION_ID:
                provider_session_id = step.content.get("session_id")
                if provider_session_id:
                    session.agent_session_id = str(provider_session_id)
                    await rt.store.save_session(session)
            await rt.record_step(session, step)
            if (
                session.steps_count > 0
                and session.steps_count % 5 == 0
                and run.task.spin_detection.enabled
            ):
                report = await rt.check_spin(session, run)
                if report and report.detected:
                    if report.action != "warn_and_inject_diagnostic":
                        spin_detected = True
                        cancel_token.cancel(reason=f"spin:{report.layer}")
            if session.steps_count >= run.task.resources.max_steps_per_session:
                cancel_token.cancel(reason="session_step_limit")

        try:
            agent = build_agent(agent_config or run.task.agent)
            workspace = Workspace(
                path=workspace_path or run.workspace_path,
                env=rt.workspace_env(run),
            )
            invocation = agent.run_session(
                session_prompt=prompt,
                workspace=workspace,
                resume_session_id=resume_session_id,
                on_step=on_step,
                cancel_token=cancel_token,
                session_id=session.id,
            )
            timeout = timeout_seconds
            if timeout is None:
                timeout = run.task.resources.max_minutes_per_session * 60
            try:
                result = await asyncio.wait_for(invocation, timeout=timeout)
            except TimeoutError:
                cancel_token.cancel("attempt_timeout")
                result = SessionRunResult(
                    status=SessionStatus.TIMEOUT,
                    error=f"attempt exceeded {timeout:g} seconds",
                )
            except Exception as exc:
                result = SessionRunResult(
                    status=SessionStatus.ERRORED,
                    error=str(exc),
                )

            assert result is not None
            if cancel_token.cancelled and result.status in {
                SessionStatus.COMPLETED,
                SessionStatus.TIMEOUT,
            }:
                result = result.model_copy(
                    update={
                        "status": (
                            SessionStatus.SPIN
                            if "spin" in cancel_token.reason
                            else SessionStatus.TIMEOUT
                        ),
                        "error": cancel_token.reason,
                    }
                )
            if result.agent_session_id:
                session.agent_session_id = result.agent_session_id
                await rt.store.save_session(session)
            rt.charge(result)
            if result.status == SessionStatus.COMPLETED:
                for stage in validator_stages:
                    decisions.extend(
                        await rt.run_validators(run, session, when=stage)
                    )
        except BudgetExceeded:
            raise
        except Exception as exc:
            result = SessionRunResult(status=SessionStatus.ERRORED, error=str(exc))
        finally:
            cleanup_errors: list[str] = []
            for callback in cleanup:
                try:
                    await callback()
                except Exception as exc:
                    cleanup_errors.append(str(exc))
            if result is None:
                result = SessionRunResult(
                    status=SessionStatus.ERRORED,
                    error="attempt ended without an agent result",
                )
            if cleanup_errors and result.status == SessionStatus.COMPLETED:
                result = result.model_copy(
                    update={
                        "status": SessionStatus.ERRORED,
                        "error": f"cleanup failed: {'; '.join(cleanup_errors)}",
                    }
                )
            await rt.end_session(session, result.status)

        return AttemptResult(
            session=session,
            agent=result,
            decisions=decisions,
            spin_detected=spin_detected,
        )
