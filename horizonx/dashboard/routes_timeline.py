from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from horizonx.storage.sqlite import SqliteStore

from .deps import get_store
from .timeline import TimelineEventDetail, TimelinePage, TimelinePlayback, TimelineProjection

router = APIRouter()


def _missing(error: KeyError) -> HTTPException:
    return HTTPException(status_code=404, detail=str(error))


@router.get("/runs/{run_id}/timeline", response_model=TimelinePage)
async def timeline_page(
    run_id: str,
    after: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    store: SqliteStore = Depends(get_store),
) -> TimelinePage:
    try:
        return await TimelineProjection(store).page(run_id, after=after, limit=limit)
    except KeyError as exc:
        raise _missing(exc) from None


@router.get("/runs/{run_id}/timeline/playback", response_model=TimelinePlayback)
async def timeline_playback(
    run_id: str,
    sequence: int = Query(..., ge=0),
    store: SqliteStore = Depends(get_store),
) -> TimelinePlayback:
    try:
        return await TimelineProjection(store).playback(run_id, sequence=sequence)
    except KeyError as exc:
        raise _missing(exc) from None


@router.get("/runs/{run_id}/timeline/{sequence}", response_model=TimelineEventDetail)
async def timeline_event_detail(
    run_id: str,
    sequence: int,
    store: SqliteStore = Depends(get_store),
) -> TimelineEventDetail:
    if sequence < 0:
        raise HTTPException(status_code=422, detail="sequence must be non-negative")
    try:
        return await TimelineProjection(store).event_detail(run_id, sequence)
    except KeyError as exc:
        raise _missing(exc) from None
