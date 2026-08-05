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
    try:
        if store is not None:
            while True:
                replay = (
                    await store.list_events(
                        run_id, after_sequence=cursor, limit=1000
                    )
                    if run_id is not None
                    else await store.list_all_events(
                        after_sequence=cursor, limit=1000
                    )
                )
                for event in replay:
                    cursor = max(cursor, event.sequence or 0)
                    yield {
                        "id": str(event.sequence),
                        "event": event.type,
                        "data": event.model_dump_json(),
                    }
                if len(replay) < 1000:
                    break
        while True:
            try:
                event = await asyncio.wait_for(asyncio.shield(live_task), timeout=0.1)
                live_task = asyncio.ensure_future(anext(subscription))
            except TimeoutError:
                if store is None:
                    continue
                replay = (
                    await store.list_events(run_id, after_sequence=cursor, limit=1000)
                    if run_id is not None
                    else await store.list_all_events(after_sequence=cursor, limit=1000)
                )
                for persisted in replay:
                    cursor = max(cursor, persisted.sequence or 0)
                    yield {
                        "id": str(persisted.sequence),
                        "event": persisted.type,
                        "data": persisted.model_dump_json(),
                    }
                continue
            if event.sequence is None and store is not None:
                event = await store.append_event(event)
            if event.sequence is not None and event.sequence <= cursor:
                continue
            cursor = event.sequence or cursor
            yield {
                "id": str(event.sequence) if event.sequence is not None else event.id,
                "event": event.type,
                "data": event.model_dump_json(),
            }
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
