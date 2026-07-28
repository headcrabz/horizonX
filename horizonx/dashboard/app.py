"""FastAPI app factory for the HorizonX web dashboard."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from horizonx.core.event_bus import InMemoryBus
from horizonx.core.runtime import Runtime
from horizonx.storage.sqlite import SqliteStore

from .routes_events import router as events_router
from .routes_hitl import router as hitl_router
from .routes_launch import router as launch_router
from .routes_runs import router as runs_router
from .routes_templates import router as templates_router


def create_app(
    db_path: str | Path = "horizonx.db",
    workspace_root: str | Path = "./horizonx-workspaces",
) -> FastAPI:
    db_path = Path(db_path)
    workspace_root = Path(workspace_root)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.store = SqliteStore(db_path)
        app.state.bus = InMemoryBus()
        app.state.runtime = Runtime(
            store=app.state.store,
            bus=app.state.bus,
            workspace_root=workspace_root,
        )
        from horizonx.dashboard.recovery import recover_pending_runs
        await recover_pending_runs(app.state.store, app.state.runtime)
        yield

    app = FastAPI(title="HorizonX Dashboard", version="0.1.0", lifespan=lifespan)

    app.include_router(runs_router, prefix="/api")
    app.include_router(events_router, prefix="/api")
    app.include_router(hitl_router, prefix="/api")
    app.include_router(launch_router, prefix="/api")
    app.include_router(templates_router, prefix="/api")

    @app.get("/api/health")
    async def health() -> dict[str, object]:
        return {"ok": True, "version": "0.1.0"}

    # Static files last so they don't shadow API routes
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app
