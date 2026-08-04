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
from horizonx.runtime.watchdog import StallOutcome, StallWatchdog

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
        total_hours = run.task.resources.max_total_hours
        remaining_total_seconds: float | None = None
        if total_hours is not None:
            elapsed = run.cumulative.wall_seconds
            remaining_total_seconds = total_hours * 3600 - elapsed
            if remaining_total_seconds <= 0:
                raise BudgetExceeded("total run time limit reached")
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
            run.cumulative.attempts_count += 1
            await rt.store.save_run(run)
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
        streamed_tokens = 0
        result: SessionRunResult | None = None
        limits = run.task.resources
        watchdog: StallWatchdog | None = None
        if limits.stall_soft_seconds > 0 and limits.stall_hard_seconds > 0:
            watchdog = StallWatchdog(
                soft_seconds=limits.stall_soft_seconds,
                hard_seconds=limits.stall_hard_seconds,
                poll_interval=max(
                    0.001,
                    min(
                        1.0,
                        limits.stall_soft_seconds / 4,
                        limits.stall_hard_seconds / 4,
                    ),
                ),
            )

        async def on_step(step: Step) -> None:
            nonlocal spin_detected, streamed_tokens
            if watchdog is not None:
                watchdog.notify_activity()
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
            if step.type == StepType.USAGE:
                usage = step.content.get("usage") or step.content
                if isinstance(usage, dict):
                    observed_tokens = sum(
                        int(usage.get(key) or 0)
                        for key in (
                            "input_tokens",
                            "output_tokens",
                            "cached_input_tokens",
                            "cache_creation_input_tokens",
                            "cache_read_input_tokens",
                        )
                    )
                    if step.content.get("usage_mode") == "cumulative":
                        streamed_tokens = observed_tokens
                    else:
                        streamed_tokens += observed_tokens
                session_token_limit = run.task.resources.max_tokens_per_session
                if (
                    session_token_limit is not None
                    and streamed_tokens >= session_token_limit
                ):
                    cancel_token.cancel(reason="session_token_limit")
                total_token_limit = run.task.resources.max_total_tokens
                cumulative_tokens = (
                    run.cumulative.tokens_in
                    + run.cumulative.tokens_out
                    + run.cumulative.cache_creation_tokens
                    + run.cumulative.cache_read_tokens
                )
                if (
                    total_token_limit is not None
                    and cumulative_tokens + streamed_tokens >= total_token_limit
                ):
                    cancel_token.cancel(reason="total_token_limit")
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
            session_task = asyncio.create_task(invocation)

            async def on_stall_nudge(reason: str) -> None:
                await rt.bus.publish(
                    Event(
                        type="session.stall_nudge",
                        run_id=run.id,
                        attempt_id=attempt.id,
                        session_id=session.id,
                        goal_id=attempt.goal_id,
                        payload={"reason": reason},
                    )
                )

            async def on_watchdog_tick() -> None:
                await rt.checkpoint_resources(run)

            watchdog_task = (
                asyncio.create_task(
                    watchdog.run(
                        session_task,
                        on_nudge=on_stall_nudge,
                        on_tick=on_watchdog_tick,
                    )
                )
                if watchdog is not None
                else None
            )
            timeout = timeout_seconds
            if timeout is None:
                timeout = run.task.resources.max_minutes_per_session * 60
            if remaining_total_seconds is not None:
                timeout = min(timeout, remaining_total_seconds)
            try:
                result = await asyncio.wait_for(
                    asyncio.shield(session_task), timeout=timeout
                )
                if watchdog_task is not None:
                    await watchdog_task
            except asyncio.CancelledError:
                current_task = asyncio.current_task()
                if current_task is not None and current_task.cancelling():
                    cancel_token.cancel("run_cancelled")
                    session_task.cancel()
                    if watchdog_task is not None:
                        watchdog_task.cancel()
                    await asyncio.gather(
                        session_task,
                        *([watchdog_task] if watchdog_task is not None else []),
                        return_exceptions=True,
                    )
                    raise
                watchdog_outcome = (
                    await watchdog_task if watchdog_task is not None else None
                )
                if watchdog_outcome != StallOutcome.HARD_ABORT:
                    raise
                cancel_token.cancel("stall_hard_timeout")
                await rt.bus.publish(
                    Event(
                        type="session.stall_abort",
                        run_id=run.id,
                        attempt_id=attempt.id,
                        session_id=session.id,
                        goal_id=attempt.goal_id,
                        payload={"reason": cancel_token.reason},
                    )
                )
                result = SessionRunResult(
                    status=SessionStatus.TIMEOUT,
                    error=cancel_token.reason,
                )
            except TimeoutError:
                cancel_token.cancel("attempt_timeout")
                session_task.cancel()
                await asyncio.gather(session_task, return_exceptions=True)
                result = SessionRunResult(
                    status=SessionStatus.TIMEOUT,
                    error=f"attempt exceeded {timeout:g} seconds",
                )
            except Exception as exc:
                result = SessionRunResult(
                    status=SessionStatus.ERRORED,
                    error=str(exc),
                )
            finally:
                if watchdog_task is not None and not watchdog_task.done():
                    watchdog_task.cancel()
                    await asyncio.gather(watchdog_task, return_exceptions=True)

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
            session.tokens_used = (
                result.tokens_in
                + result.tokens_out
                + result.cache_creation_tokens
                + result.cache_read_tokens
            )
            run.cumulative.steps_count += session.steps_count
            run.cumulative.housekeeping_steps += session.housekeeping_steps
            await rt.store.save_session(session)
            await rt.charge(
                run,
                result,
                attempt_id=attempt.id,
                session_id=session.id,
            )
            max_session_tokens = run.task.resources.max_tokens_per_session
            if (
                max_session_tokens is not None
                and session.tokens_used >= max_session_tokens
            ):
                raise BudgetExceeded(
                    f"session token limit reached: "
                    f"{session.tokens_used}/{max_session_tokens}"
                )
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
