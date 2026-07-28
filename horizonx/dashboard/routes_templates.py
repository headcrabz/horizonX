"""GE-02 — GET /api/templates/goal-nodes endpoint."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.get("/templates/goal-nodes")
async def list_goal_node_templates() -> list[dict[str, Any]]:
    """Return all 15 bundled goal node templates."""
    from horizonx.templates.loader import TemplateLibrary
    try:
        return TemplateLibrary.load().all()
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Template library unavailable: {exc}",
        ) from exc


@router.get("/templates/goal-nodes/{template_id}")
async def get_goal_node_template(template_id: str) -> dict[str, Any]:
    """Return a single goal node template by id."""
    from horizonx.templates.loader import TemplateLibrary
    tpl = TemplateLibrary.load().get(template_id)
    if tpl is None:
        raise HTTPException(status_code=404, detail=f"Template {template_id!r} not found")
    return tpl
