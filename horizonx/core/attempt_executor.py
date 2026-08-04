"""One shared lifecycle for bounded agent attempts."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from horizonx.agents.base import CancelToken, Workspace
from horizonx.agents.registry import build_agent
from horizonx.core.attempt_result import AttemptResult
from horizonx.core.event_bus import Event
from horizonx.core.governor import BudgetExceeded
from horizonx.core.types import (
    AgentConfig,
    AttemptRecord,
    AttemptStatus,
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
        recovery_value = (
            rt.take_recovery_context(run.id)
            if hasattr(rt, "take_recovery_context")
            else None
        )
        recovery_context = (
            recovery_value if isinstance(recovery_value, dict) else None
        )
        configured_agent = agent_config or run.task.agent
        effective_resume_session_id = resume_session_id or (
            recovery_context.get("provider_session_id")
            if recovery_context
            else None
        )
        snapshot_value = (
            rt.workspace_snapshot(run)
            if hasattr(rt, "workspace_snapshot")
            else {}
        )
        workspace_environment = rt.workspace_env(run)
        from horizonx.security.environment_policy import trust_boundary_metadata

        workspace_snapshot = (
            dict(snapshot_value) if isinstance(snapshot_value, dict) else {}
        )
        workspace_snapshot["trust_boundary"] = trust_boundary_metadata(
            workspace_environment, configured_agent
        )
        attempt = AttemptRecord(
            lineage_id=(
                recovery_context.get("lineage_id") if recovery_context else None
            ),
            run_id=run.id,
            goal_id=target_goal.id if target_goal else None,
            session_id=session.id,
            status=AttemptStatus.RUNNING,
            provider=configured_agent.type,
            model=configured_agent.model,
            workspace_path=workspace_path or run.workspace_path,
            workspace_snapshot=workspace_snapshot,
            retry_cause=(
                recovery_context.get("retry_cause") if recovery_context else None
            ),
            max_attempts=target_goal.max_attempts if target_goal else 3,
        )
        create_result = rt.store.create_attempt(attempt)
        if inspect.isawaitable(create_result):
            attempt = await create_result
            await rt.bus.publish(
                Event(
                    type="attempt.started",
                    run_id=run.id,
                    attempt_id=attempt.id,
                    session_id=session.id,
                    goal_id=attempt.goal_id,
                    payload={
                        "ordinal": attempt.ordinal,
                        "provider": attempt.provider,
                        "resuming_provider": bool(effective_resume_session_id),
                    },
                )
            )
        cancel_token = CancelToken()
        decisions: list[GateDecision] = []
        spin_detected = False
        result: SessionRunResult | None = None

        async def on_step(step: Step) -> None:
            nonlocal spin_detected
            step.session_id = session.id
            from horizonx.security.environment_policy import redact_secrets

            redacted_content = redact_secrets(step.content, workspace_environment)
            if isinstance(redacted_content, dict):
                step.content = redacted_content
            if step.type == StepType.SESSION_ID:
                provider_session_id = step.content.get("session_id")
                if provider_session_id:
                    session.agent_session_id = str(provider_session_id)
                    await rt.store.save_session(session)
                    provider_update = rt.store.set_attempt_provider_session(
                        attempt.id, str(provider_session_id)
                    )
                    if inspect.isawaitable(provider_update):
                        attempt.provider_session_id = str(provider_session_id)
                        await provider_update
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
            agent = build_agent(configured_agent)
            workspace = Workspace(
                path=workspace_path or run.workspace_path,
                env=rt.workspace_env(run),
            )
            invocation_kwargs: dict[str, Any] = {
                "resume_session_id": effective_resume_session_id,
                "on_step": on_step,
                "cancel_token": cancel_token,
            }
            parameters = inspect.signature(agent.run_session).parameters
            if "session_id" in parameters or any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            ):
                invocation_kwargs["session_id"] = session.id
            invocation = agent.run_session(
                session_prompt=prompt,
                workspace=workspace,
                **invocation_kwargs,
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
                provider_update = rt.store.set_attempt_provider_session(
                    attempt.id, result.agent_session_id
                )
                if inspect.isawaitable(provider_update):
                    attempt.provider_session_id = result.agent_session_id
                    await provider_update
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
            attempt_status = (
                AttemptStatus.COMPLETED
                if result.status == SessionStatus.COMPLETED
                else AttemptStatus.ABORTED
                if result.status == SessionStatus.TIMEOUT
                and result.error == "run_cancelled"
                else AttemptStatus.FAILED
            )
            transition_result = rt.store.transition_attempt(
                attempt.id,
                attempt_status,
                error=result.error,
                retry_cause=(
                    result.error
                    if attempt_status == AttemptStatus.FAILED
                    else attempt.retry_cause
                ),
            )
            if inspect.isawaitable(transition_result):
                attempt = await transition_result
                await rt.bus.publish(
                    Event(
                        type=(
                            "attempt.completed"
                            if attempt_status == AttemptStatus.COMPLETED
                            else "attempt.failed"
                        ),
                        run_id=run.id,
                        attempt_id=attempt.id,
                        session_id=session.id,
                        goal_id=attempt.goal_id,
                        payload={
                            "status": attempt.status.value,
                            "error": attempt.error,
                        },
                    )
                )

        return AttemptResult(
            attempt=attempt,
            session=session,
            agent=result,
            decisions=decisions,
            spin_detected=spin_detected,
        )
