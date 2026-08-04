"""Import boundaries for optional dashboard dependencies."""

from __future__ import annotations

import importlib
import sys


def test_recovery_import_does_not_require_fastapi(monkeypatch) -> None:
    """Durable recovery remains available in the base installation."""
    for module_name in (
        "fastapi",
        "horizonx.dashboard",
        "horizonx.dashboard.app",
        "horizonx.dashboard.recovery",
    ):
        monkeypatch.delitem(sys.modules, module_name, raising=False)
    monkeypatch.setitem(sys.modules, "fastapi", None)

    recovery = importlib.import_module("horizonx.dashboard.recovery")

    assert callable(recovery.reconcile_runs)
