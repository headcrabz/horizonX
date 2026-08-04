"""Optional web dashboard package.

Recovery helpers must remain importable in the base installation, where the
FastAPI dashboard extra is intentionally absent.
"""

from __future__ import annotations

from typing import Any

__all__ = ["create_app"]


def __getattr__(name: str) -> Any:
    if name == "create_app":
        from horizonx.dashboard.app import create_app

        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
