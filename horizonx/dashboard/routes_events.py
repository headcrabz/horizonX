from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from fastapi import APIRouter, Depends, Request
from sse_starlette.sse import EventSourceResponse

from horizonx.core.event_bus import InMemoryBus

from .deps import get_bus

router = APIRouter()


async def _event_gen(
    bus: InMemoryBus,
    predicate: Any = None,
) -> AsyncGenerator[dict[str, str], None]:
    async for event in bus.subscribe(predicate=predicate):
        yield {"event": event.type, "data": event.model_dump_json()}


@router.get("/runs/{run_id}/events")
async def run_events(
    run_id: str,
    request: Request,
    bus: InMemoryBus = Depends(get_bus),
) -> EventSourceResponse:
    return EventSourceResponse(
        _event_gen(bus, predicate=lambda e: e.run_id == run_id)
    )


@router.get("/events")
async def all_events(
    request: Request,
    bus: InMemoryBus = Depends(get_bus),
) -> EventSourceResponse:
    return EventSourceResponse(_event_gen(bus))
