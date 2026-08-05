"""Idempotent reconciliation plans derived from durable run and attempt state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from pydantic import BaseModel

from horizonx.core.event_bus import Event
from horizonx.core.leases import LeaseManager
from horizonx.core.operator_commands import OperatorCommandKind, hitl_resolved_event
from horizonx.core.types import (
    TERMINAL_ATTEMPT_STATUSES,
    AttemptRecord,
    AttemptStatus,
    LeaseRecord,
    Run,
    RunStatus,
)


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
                    if cancelled["request_id"] is not None:
                        await self.store.append_event(
                            hitl_resolved_event(
                                run_id=run.id,
                                request_id=cancelled["request_id"],
                                action="abort",
                                actor=cancelled["actor"],
                                instruction=cancelled["instruction"],
                            )
                        )
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
                    run.status = RunStatus.PAUSED_HITL
                    run.completed_at = None
                    await self.store.save_run(run)
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

    async def _recover_paused_hitl(
        self,
        run: Run,
        latest: AttemptRecord | None,
        lease: LeaseRecord,
        now: datetime,
    ) -> RecoveryDecision | None:
        """Apply durable operator state left behind when a HITL waiter died."""
        requests = await self.store.list_hitl_events(run.id)
        request = requests[-1] if requests else None
        commands = await self.store.list_operator_commands(
            run.id, unconsumed_only=True
        )
        if request is None:
            return None
        decision_commands = [
            command
            for command in commands
            if command.kind == OperatorCommandKind.DECISION
            and command.payload.get("request_id") == request["id"]
        ]
        if request["resolved_at"] is None and decision_commands:
            command = decision_commands[0]
            request, _ = await self.store.resolve_hitl_event(
                request["id"],
                action=str(command.payload["action"]),
                actor=command.actor,
                reason=command.reason,
                instruction=command.instruction,
                idempotency_key=command.idempotency_key,
            )
        if request["resolved_at"] is None:
            return None
        for command in decision_commands:
            await self.store.consume_operator_command(
                command.id, attempt_id=latest.id if latest else None
            )
        if request["decision"] == "abort":
            if latest is not None and latest.status not in TERMINAL_ATTEMPT_STATUSES:
                await self.store.transition_attempt(
                    latest.id,
                    AttemptStatus.ABORTED,
                    error="operator_aborted_during_hitl",
                )
            await self.store.transition_run(run.id, RunStatus.ABORTED)
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
        run.status = RunStatus.RUNNING
        run.completed_at = None
        await self.store.save_run(run)
        return await self._decision(run, latest, lease, now)

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
