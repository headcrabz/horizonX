"""Idempotent reconciliation plans derived from durable run and attempt state."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, cast

from pydantic import BaseModel

from horizonx.core.event_bus import Event
from horizonx.core.leases import LeaseManager
from horizonx.core.operator_commands import OperatorCommandKind
from horizonx.core.types import (
    TERMINAL_ATTEMPT_STATUSES,
    TERMINAL_RUN_STATUSES,
    AttemptRecord,
    AttemptStatus,
    LeaseRecord,
    Run,
    RunStatus,
)

_RECOVERY_AMBIGUITY_TRIGGER = "recovery_ambiguous_completion"


class RecoveryAction(str, Enum):
    RESTART_RUN = "restart_run"
    NEW_ATTEMPT = "new_attempt"
    RESUME_PROVIDER = "resume_provider"


class RecoveryDecision(BaseModel):
    run_id: str
    action: RecoveryAction
    reason: str
    lease: LeaseRecord
    previous_attempt_id: str | None = None
    lineage_id: str | None = None
    provider_session_id: str | None = None
    not_before: datetime | None = None


class RetryPolicy(BaseModel):
    base_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 300.0

    def next_eligible(self, attempt: AttemptRecord, now: datetime) -> datetime:
        delay = min(
            self.max_backoff_seconds,
            self.base_backoff_seconds * (2 ** attempt.retry_count),
        )
        return now + timedelta(seconds=delay)


def adapter_supports_resume(run: Run) -> bool:
    configured = run.task.agent.extra.get("supports_resume")
    if configured is not None:
        return bool(configured)
    if run.task.agent.type == "claude_code":
        return not bool(run.task.agent.extra.get("no_session_persistence", False))
    if run.task.agent.type == "codex":
        return not bool(run.task.agent.extra.get("ephemeral", False))
    return False


class RecoveryCoordinator:
    def __init__(
        self,
        store: Any,
        *,
        lease_ttl_seconds: float = 30.0,
        retry_policy: RetryPolicy | None = None,
    ):
        self.store = store
        self.lease_ttl_seconds = lease_ttl_seconds
        self.leases = LeaseManager(store)
        self.retry_policy = retry_policy or RetryPolicy()

    async def plan(
        self, *, owner: str, now: datetime | None = None
    ) -> list[RecoveryDecision]:
        scan_time = now or datetime.now(UTC)
        decisions: list[RecoveryDecision] = []
        for run in await self.store.list_nonterminal_runs():
            latest = await self.store.latest_attempt(run.id)
            if (
                latest is not None
                and latest.status == AttemptStatus.WAITING_RETRY
                and latest.next_eligible_at is not None
                and latest.next_eligible_at > scan_time
            ):
                continue

            resource_id = f"run:{run.id}"
            lease = await self.leases.acquire(
                resource_id,
                owner=owner,
                ttl_seconds=self.lease_ttl_seconds,
                now=scan_time,
            )
            if lease is None:
                continue

            lease_handed_off = False
            try:
                pending_commands = await self.store.list_operator_commands(
                    run.id, unconsumed_only=True
                )
                pending_cancel = next(
                    (
                        command
                        for command in pending_commands
                        if command.kind == OperatorCommandKind.CANCEL
                    ),
                    None,
                )
                if pending_cancel is not None:
                    cancelled = await self.store.apply_cancel_command(pending_cancel.id)
                    await self.store.append_event(
                        Event(
                            id=f"recovery-{run.id}-{lease.version}",
                            type="recovery.planned",
                            run_id=run.id,
                            attempt_id=cancelled["attempt_id"],
                            payload={
                                "action": "abort_run",
                                "reason": "operator_cancelled_before_recovery",
                                "lease_owner": lease.owner,
                                "lease_version": lease.version,
                            },
                        )
                    )
                    continue
                if run.status == RunStatus.PAUSED_HITL or (
                    latest is not None and latest.status == AttemptStatus.PAUSED_HITL
                ):
                    hitl_decision = await self._recover_paused_hitl(
                        run, latest, lease, scan_time
                    )
                    if hitl_decision is not None:
                        await self.store.append_event(
                            Event(
                                id=f"recovery-{run.id}-{lease.version}",
                                type="recovery.planned",
                                run_id=run.id,
                                attempt_id=latest.id if latest else None,
                                payload={
                                    "action": hitl_decision.action.value,
                                    "reason": hitl_decision.reason,
                                    "lease_owner": lease.owner,
                                    "lease_version": lease.version,
                                },
                            )
                        )
                        decisions.append(hitl_decision)
                        lease_handed_off = True
                    continue
                if latest is not None and latest.status == AttemptStatus.COMPLETED:
                    if (
                        not run.task.hitl.enabled
                        or _RECOVERY_AMBIGUITY_TRIGGER not in run.task.hitl.triggers
                    ):
                        await self.store.transition_run(run.id, RunStatus.FAILED)
                        await self.store.append_event(
                            Event(
                                id=f"recovery-{run.id}-{lease.version}",
                                type="recovery.planned",
                                run_id=run.id,
                                attempt_id=latest.id,
                                payload={
                                    "action": "fail_run",
                                    "reason": (
                                        "completed_attempt_reconciliation_unavailable"
                                    ),
                                    "lease_owner": lease.owner,
                                    "lease_version": lease.version,
                                },
                            )
                        )
                        continue
                    try:
                        await self.store.enter_hitl(
                            run.id,
                            _RECOVERY_AMBIGUITY_TRIGGER,
                            {
                                "attempt_id": latest.id,
                                "reason": "completed_attempt_without_terminal_run",
                            },
                            actor="recovery-coordinator",
                            instruction=(
                                "Approve to accept the completed attempt, or abort the run."
                            ),
                        )
                    except Exception as exc:
                        if (
                            getattr(exc, "run_id", None) == run.id
                            and getattr(exc, "status", None)
                            in {status.value for status in TERMINAL_RUN_STATUSES}
                        ):
                            continue
                        raise
                    await self.store.append_event(
                        Event(
                            id=f"recovery-{run.id}-{lease.version}",
                            type="recovery.planned",
                            run_id=run.id,
                            attempt_id=latest.id,
                            payload={
                                "action": "pause_for_reconciliation",
                                "reason": "completed_attempt_without_terminal_run",
                                "lease_owner": lease.owner,
                                "lease_version": lease.version,
                            },
                        )
                    )
                    continue
                if latest is not None and latest.status == AttemptStatus.ABORTED:
                    await self.store.transition_run(run.id, RunStatus.ABORTED)
                    await self.store.append_event(
                        Event(
                            id=f"recovery-{run.id}-{lease.version}",
                            type="recovery.planned",
                            run_id=run.id,
                            attempt_id=latest.id,
                            payload={
                                "action": "abort_run",
                                "reason": "attempt_was_aborted",
                                "lease_owner": lease.owner,
                                "lease_version": lease.version,
                            },
                        )
                    )
                    continue
                if (
                    latest is not None
                    and latest.retry_count + 1 >= latest.max_attempts
                ):
                    await self.store.transition_attempt(
                        latest.id,
                        AttemptStatus.INTERRUPTED,
                        error="worker disappeared and retry limit is exhausted",
                        retry_cause="retry_limit_exhausted",
                    )
                    await self.store.transition_run(run.id, RunStatus.FAILED)
                    await self.store.append_event(
                        Event(
                            id=f"recovery-{run.id}-{lease.version}",
                            type="recovery.planned",
                            run_id=run.id,
                            attempt_id=latest.id,
                            payload={
                                "action": "fail_run",
                                "reason": "retry_limit_exhausted",
                                "lease_owner": lease.owner,
                                "lease_version": lease.version,
                            },
                        )
                    )
                    continue

                decision = await self._decision(run, latest, lease, scan_time)
                await self.store.append_event(
                    Event(
                        id=f"recovery-{run.id}-{lease.version}",
                        type="recovery.planned",
                        run_id=run.id,
                        attempt_id=decision.previous_attempt_id,
                        payload={
                            "action": decision.action.value,
                            "reason": decision.reason,
                            "lease_owner": lease.owner,
                            "lease_version": lease.version,
                        },
                    )
                )
                decisions.append(decision)
                lease_handed_off = True
            finally:
                if not lease_handed_off:
                    await self.store.release_lease(
                        lease.resource_id, lease.owner, lease.version
                    )
        return decisions

    async def _apply_recovery_hitl_transition(
        self,
        run: Run,
        request_id: str,
        to_status: RunStatus,
        lease: LeaseRecord,
        latest: AttemptRecord | None,
    ) -> Run | None:
        from horizonx.storage.sqlite import HITLTransitionError

        try:
            return cast(
                Run,
                await self.store.apply_hitl_decision(
                    run.id,
                    expected_request_id=request_id,
                    to_status=to_status,
                ),
            )
        except HITLTransitionError:
            persisted = await self.store.load_run(run.id)
            if persisted.status in TERMINAL_RUN_STATUSES:
                action = "preserve_terminal"
                reason = "terminal_won_before_hitl_consumption"
            else:
                persisted = await self.store.transition_run(run.id, RunStatus.FAILED)
                action = "fail_run"
                reason = "active_hitl_request_changed_before_consumption"
            await self.store.append_event(
                Event(
                    id=f"recovery-{run.id}-{lease.version}",
                    type="recovery.planned",
                    run_id=run.id,
                    attempt_id=latest.id if latest else None,
                    payload={
                        "action": action,
                        "reason": reason,
                        "status": persisted.status.value,
                        "lease_owner": lease.owner,
                        "lease_version": lease.version,
                    },
                )
            )
            return None

    async def _recover_paused_hitl(
        self,
        run: Run,
        latest: AttemptRecord | None,
        lease: LeaseRecord,
        now: datetime,
    ) -> RecoveryDecision | None:
        """Apply durable operator state left behind when a HITL waiter died."""
        from horizonx.storage.sqlite import (
            HITLTransitionError,
            OperatorCommandConflict,
        )

        request_id = run.active_hitl_request_id
        request = None
        if request_id is not None:
            try:
                candidate = await self.store.find_hitl_event(request_id)
            except KeyError:
                candidate = None
            if candidate is not None and candidate["run_id"] == run.id:
                request = candidate
        commands = await self.store.list_operator_commands(
            run.id, unconsumed_only=True
        )
        if request is None:
            await self.store.transition_run(run.id, RunStatus.FAILED)
            await self.store.append_event(
                Event(
                    id=f"recovery-{run.id}-{lease.version}",
                    type="recovery.planned",
                    run_id=run.id,
                    attempt_id=latest.id if latest else None,
                    payload={
                        "action": "fail_run",
                        "reason": (
                            "paused_hitl_missing_active_request"
                            if request_id is None
                            else "paused_hitl_invalid_active_request"
                        ),
                        "lease_owner": lease.owner,
                        "lease_version": lease.version,
                    },
                )
            )
            return None
        decision_commands = [
            command
            for command in commands
            if command.kind == OperatorCommandKind.DECISION
            and command.payload.get("request_id") == request["id"]
        ]
        authoritative_result = None
        if request["resolved_at"] is None and decision_commands:
            command = decision_commands[0]
            try:
                authoritative_result = (
                    await self.store.submit_active_hitl_decision(command)
                )
            except (OperatorCommandConflict, HITLTransitionError):
                try:
                    authoritative_result = (
                        await self.store.load_authoritative_active_hitl_decision(
                            run.id, request["id"]
                        )
                    )
                except (OperatorCommandConflict, HITLTransitionError):
                    await self._fail_recovery_hitl_resolution(
                        run,
                        lease,
                        latest,
                        reason=(
                            "active_hitl_resolution_invalid_after_submission_conflict"
                        ),
                    )
                    return None
            request = authoritative_result.request
        if request["resolved_at"] is None:
            return None
        if authoritative_result is None:
            try:
                authoritative_result = (
                    await self.store.load_authoritative_active_hitl_decision(
                        run.id, request["id"]
                    )
                )
            except (OperatorCommandConflict, HITLTransitionError):
                await self._fail_recovery_hitl_resolution(
                    run,
                    lease,
                    latest,
                    reason="active_hitl_resolution_is_not_authoritative",
                )
                return None
            request = authoritative_result.request
        await self.store.consume_operator_command(
            authoritative_result.command.id,
            attempt_id=latest.id if latest else None,
        )
        if request["decision"] == "abort":
            if latest is not None and latest.status not in TERMINAL_ATTEMPT_STATUSES:
                await self.store.transition_attempt(
                    latest.id,
                    AttemptStatus.ABORTED,
                    error="operator_aborted_during_hitl",
                )
            applied = await self._apply_recovery_hitl_transition(
                run, request["id"], RunStatus.ABORTED, lease, latest
            )
            if applied is None:
                return None
            await self.store.append_event(
                Event(
                    id=f"recovery-{run.id}-{lease.version}",
                    type="recovery.planned",
                    run_id=run.id,
                    attempt_id=latest.id if latest else None,
                    payload={
                        "action": "abort_run",
                        "reason": "hitl_resolution_aborted",
                        "lease_owner": lease.owner,
                        "lease_version": lease.version,
                    },
                )
            )
            return None
        if request["trigger"] == _RECOVERY_AMBIGUITY_TRIGGER:
            context = request.get("context") or {}
            if isinstance(context, str):
                context = json.loads(context)
            request_attempt_id = context.get("attempt_id")
            if (
                latest is None
                or latest.status != AttemptStatus.COMPLETED
                or request_attempt_id != latest.id
            ):
                status = RunStatus.FAILED
                action = "fail_run"
                reason = "recovery_ambiguity_request_stale"
            elif request["decision"] == "approve":
                status = RunStatus.COMPLETED
                action = "complete_run"
                reason = "completed_attempt_approved"
            else:
                status = RunStatus.FAILED
                action = "fail_run"
                reason = "unsupported_recovery_ambiguity_resolution"
            applied = await self._apply_recovery_hitl_transition(
                run, request["id"], status, lease, latest
            )
            if applied is None:
                return None
            await self.store.append_event(
                Event(
                    id=f"recovery-{run.id}-{lease.version}",
                    type="recovery.planned",
                    run_id=run.id,
                    attempt_id=latest.id if latest else None,
                    payload={
                        "action": action,
                        "reason": reason,
                        "decision": request["decision"],
                        "instruction": request.get("instruction") or "",
                        "lease_owner": lease.owner,
                        "lease_version": lease.version,
                    },
                )
            )
            return None
        resumed = await self._apply_recovery_hitl_transition(
            run, request["id"], RunStatus.RUNNING, lease, latest
        )
        if resumed is None:
            return None
        return await self._decision(resumed, latest, lease, now)

    async def _fail_recovery_hitl_resolution(
        self,
        run: Run,
        lease: LeaseRecord,
        latest: AttemptRecord | None,
        *,
        reason: str,
    ) -> None:
        persisted = await self.store.load_run(run.id)
        if persisted.status in TERMINAL_RUN_STATUSES:
            action = "preserve_terminal"
        else:
            persisted = await self.store.transition_run(run.id, RunStatus.FAILED)
            action = "fail_run"
        await self.store.append_event(
            Event(
                id=f"recovery-{run.id}-{lease.version}",
                type="recovery.planned",
                run_id=run.id,
                attempt_id=latest.id if latest else None,
                payload={
                    "action": action,
                    "reason": reason,
                    "status": persisted.status.value,
                    "lease_owner": lease.owner,
                    "lease_version": lease.version,
                },
            )
        )

    async def _decision(
        self,
        run: Run,
        latest: AttemptRecord | None,
        lease: LeaseRecord,
        now: datetime,
    ) -> RecoveryDecision:
        if latest is None:
            return RecoveryDecision(
                run_id=run.id,
                action=RecoveryAction.RESTART_RUN,
                reason="run_has_no_attempt",
                lease=lease,
            )

        can_resume = (
            latest.status != AttemptStatus.FAILED
            and bool(latest.provider_session_id)
            and adapter_supports_resume(run)
        )
        reason = (
            "attempt_failed"
            if latest.status == AttemptStatus.FAILED
            else (
                "provider_session_available"
                if can_resume
                else (
                    "adapter_cannot_resume"
                    if latest.provider_session_id
                    else "provider_session_unavailable"
                )
            )
        )
        if latest.goal_id is not None:
            await self.store.recover_goal_claim(
                run.id, latest.goal_id, latest.session_id
            )
        next_eligible = self.retry_policy.next_eligible(latest, now)
        if latest.status not in TERMINAL_ATTEMPT_STATUSES:
            await self.store.transition_attempt(
                latest.id,
                AttemptStatus.INTERRUPTED,
                error="worker disappeared before terminal attempt state",
                retry_cause=reason,
                next_eligible_at=next_eligible,
            )
        return RecoveryDecision(
            run_id=run.id,
            action=(
                RecoveryAction.RESUME_PROVIDER
                if can_resume
                else RecoveryAction.NEW_ATTEMPT
            ),
            reason=reason,
            lease=lease,
            previous_attempt_id=latest.id,
            lineage_id=latest.lineage_id,
            provider_session_id=(latest.provider_session_id if can_resume else None),
            not_before=next_eligible,
        )
