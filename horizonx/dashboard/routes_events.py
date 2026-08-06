from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from horizonx.core.event_bus import Event, InMemoryBus
from horizonx.storage.sqlite import SqliteStore

from .deps import get_bus, get_store

router = APIRouter()
_PAGE_SIZE = 1000


def _parse_cursor(request: Request) -> int | None:
    raw = request.headers.get("last-event-id") or request.query_params.get("cursor")
    if raw is None:
        return None
    try:
        cursor = int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="event cursor must be an integer") from None
    if cursor < 0:
        raise HTTPException(status_code=400, detail="event cursor must be non-negative")
    return cursor


async def _event_gen(
    bus: InMemoryBus,
    predicate: Any = None,
    *,
    store: SqliteStore | None = None,
    run_id: str | None = None,
    after_sequence: int | None = None,
) -> AsyncGenerator[dict[str, str], None]:
    cursor = after_sequence or 0
    subscription = cast(
        AsyncGenerator[Event, None], bus.subscribe(predicate=predicate)
    )
    live_task: asyncio.Future[Event] = asyncio.ensure_future(anext(subscription))
    await asyncio.sleep(0)

    async def drain_durable(
        *, through_sequence: int | None = None
    ) -> AsyncGenerator[Event, None]:
        nonlocal cursor
        if store is None:
            return
        while through_sequence is None or cursor < through_sequence:
            page = (
                await store.list_events(
                    run_id, after_sequence=cursor, limit=_PAGE_SIZE
                )
                if run_id is not None
                else await store.list_all_events(
                    after_sequence=cursor, limit=_PAGE_SIZE
                )
            )
            if not page:
                return
            for persisted in page:
                sequence = persisted.sequence or 0
                if sequence <= cursor:
                    continue
                cursor = sequence
                yield persisted
                if through_sequence is not None and cursor >= through_sequence:
                    return
            if len(page) < _PAGE_SIZE:
                return

    def as_sse(event: Event) -> dict[str, str]:
        return {
            "id": str(event.sequence) if event.sequence is not None else event.id,
            "event": event.type,
            "data": event.model_dump_json(),
        }

    try:
        async for persisted in drain_durable():
            yield as_sse(persisted)
        while True:
            try:
                event = await asyncio.wait_for(asyncio.shield(live_task), timeout=0.1)
                live_task = asyncio.ensure_future(anext(subscription))
            except TimeoutError:
                async for persisted in drain_durable():
                    yield as_sse(persisted)
                continue
            if event.sequence is None and store is not None:
                event = await store.append_event(event)
            if store is not None and event.sequence is not None:
                async for persisted in drain_durable(
                    through_sequence=event.sequence
                ):
                    yield as_sse(persisted)
            elif event.sequence is None or event.sequence > cursor:
                cursor = event.sequence or cursor
                yield as_sse(event)
    finally:
        live_task.cancel()
        await asyncio.gather(live_task, return_exceptions=True)
        await subscription.aclose()


@router.get("/runs/{run_id}/events")
async def run_events(
    run_id: str,
    request: Request,
    bus: InMemoryBus = Depends(get_bus),
    store: SqliteStore = Depends(get_store),
) -> EventSourceResponse:
    cursor = _parse_cursor(request)
    return EventSourceResponse(
        _event_gen(
            bus,
            predicate=lambda e: e.run_id == run_id,
            store=store,
            run_id=run_id,
            after_sequence=cursor,
        )
    )


@router.get("/events")
async def all_events(
    request: Request,
    bus: InMemoryBus = Depends(get_bus),
    store: SqliteStore = Depends(get_store),
) -> EventSourceResponse:
    cursor = _parse_cursor(request)
    return EventSourceResponse(
        _event_gen(
            bus,
            store=store,
            after_sequence=cursor,
        )
    )
