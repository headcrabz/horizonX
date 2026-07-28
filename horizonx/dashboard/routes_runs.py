from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from horizonx.storage.sqlite import SqliteStore

from .deps import get_store

router = APIRouter()


@router.get("/runs")
async def list_runs(
    limit: int = Query(50, ge=1, le=500),
    status: str | None = Query(None),
    store: SqliteStore = Depends(get_store),
) -> list[dict[str, Any]]:
    return await store.list_run_summaries(limit=limit, status=status)


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    store: SqliteStore = Depends(get_store),
) -> dict[str, Any]:
    try:
        run = await store.load_run(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"run {run_id!r} not found") from None
    return run.model_dump(mode="json")


@router.get("/runs/{run_id}/sessions")
async def list_sessions(
    run_id: str,
    store: SqliteStore = Depends(get_store),
) -> list[dict[str, Any]]:
    sessions = await store.list_sessions(run_id)
    return [s.model_dump(mode="json") for s in sessions]


@router.get("/runs/{run_id}/sessions/{session_id}/steps")
async def list_steps(
    run_id: str,
    session_id: str,
    limit: int = Query(500, ge=1, le=2000),
    after: int | None = Query(None, ge=0),
    store: SqliteStore = Depends(get_store),
) -> list[dict[str, Any]]:
    steps = await store.list_steps(session_id, limit=limit, after_sequence=after)
    return [s.model_dump(mode="json") for s in steps]


@router.get("/runs/{run_id}/goals")
async def list_goals(
    run_id: str,
    store: SqliteStore = Depends(get_store),
) -> list[dict[str, Any]]:
    goals = await store.list_goals(run_id)
    return [g.model_dump(mode="json") for g in goals]


@router.get("/runs/{run_id}/validations")
async def list_validations(
    run_id: str,
    store: SqliteStore = Depends(get_store),
) -> list[dict[str, Any]]:
    return await store.list_validations(run_id)


@router.get("/runs/{run_id}/spin-reports")
async def list_spin_reports(
    run_id: str,
    store: SqliteStore = Depends(get_store),
) -> list[dict[str, Any]]:
    return await store.list_spin_reports(run_id)


@router.get("/runs/{run_id}/decomposition-report")
async def get_decomposition_report(
    run_id: str,
    store: SqliteStore = Depends(get_store),
) -> dict[str, Any]:
    """Return the GE-01 decomposition quality report for this run.

    Re-checks the current goals.json on the fly so the report reflects
    any graph changes since the run started (e.g. after re_decompose).
    """
    from horizonx.core.decomposition_checker import DecompositionChecker
    try:
        run = await store.load_run(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"run {run_id!r} not found") from None

    goals_path = run.workspace_path / "goals.json"
    if not goals_path.exists():
        # No graph yet — return empty report
        return {"error_count": 0, "warning_count": 0, "issues": []}

    from horizonx.core.goal_graph import GoalGraph
    graph = GoalGraph.load(goals_path)
    report = DecompositionChecker().check_graph(graph, run.task)
    return report.to_dict()
