"""Versioned expiring leases for restart-safe single ownership."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, cast

from horizonx.core.types import LeaseRecord


class LeaseLostError(RuntimeError):
    """Raised in the lease holder when its ownership can no longer be renewed."""


class LeaseManager:
    def __init__(self, store: Any):
        self.store = store

    async def acquire(
        self,
        resource_id: str,
        *,
        owner: str,
        ttl_seconds: float = 30.0,
        now: datetime | None = None,
    ) -> LeaseRecord | None:
        if ttl_seconds <= 0:
            raise ValueError("lease ttl_seconds must be positive")
        return cast(
            LeaseRecord | None,
            await self.store.acquire_lease(
                resource_id,
                owner,
                ttl_seconds,
                now or datetime.now(UTC),
            ),
        )

    @asynccontextmanager
    async def maintain(
        self, lease: LeaseRecord, *, ttl_seconds: float = 30.0
    ) -> AsyncIterator[LeaseRecord]:
        """Heartbeat an acquired lease until work completes, then release it."""

        holder_task = asyncio.current_task()
        if holder_task is None:  # pragma: no cover - async contexts always have a task
            raise RuntimeError("lease maintenance requires an asyncio task")
        heartbeat_error: Exception | None = None

        async def heartbeat() -> None:
            nonlocal heartbeat_error
            interval = max(0.1, ttl_seconds / 3)
            try:
                while True:
                    await asyncio.sleep(interval)
                    renewed = await self.store.heartbeat_lease(
                        lease.resource_id,
                        lease.owner,
                        lease.version,
                        ttl_seconds,
                        datetime.now(UTC),
                    )
                    if renewed is None:
                        raise LeaseLostError(f"lease lost: {lease.resource_id}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                heartbeat_error = exc
                holder_task.cancel()

        heartbeat_task = asyncio.create_task(
            heartbeat(), name=f"lease-{lease.resource_id}"
        )
        try:
            try:
                yield lease
            except asyncio.CancelledError:
                if heartbeat_error is not None:
                    raise LeaseLostError(
                        f"lease lost: {lease.resource_id}"
                    ) from heartbeat_error
                raise
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            await self.store.release_lease(
                lease.resource_id, lease.owner, lease.version
            )
