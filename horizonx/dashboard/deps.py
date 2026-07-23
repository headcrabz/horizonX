from __future__ import annotations

from fastapi import Request

from horizonx.core.event_bus import InMemoryBus
from horizonx.core.runtime import Runtime
from horizonx.storage.sqlite import SqliteStore


def get_store(request: Request) -> SqliteStore:
    return request.app.state.store  # type: ignore[no-any-return]


def get_bus(request: Request) -> InMemoryBus:
    return request.app.state.bus  # type: ignore[no-any-return]


def get_runtime(request: Request) -> Runtime:
    return request.app.state.runtime  # type: ignore[no-any-return]
