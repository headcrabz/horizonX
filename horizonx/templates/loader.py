"""GE-02 — Goal node template library loader.

Templates are defined in goal_nodes.yaml and shipped with the package.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

_TEMPLATES_DIR = Path(__file__).parent


def _load_yaml(path: Path) -> list[dict[str, Any]]:
    try:
        import yaml
    except ImportError as exc:
        raise ImportError(
            "PyYAML is required for goal node templates. "
            "Install it with: pip install horizonx[templates]"
        ) from exc
    with path.open() as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, list) else []


class TemplateLibrary:
    """Accessor for the bundled goal node template library."""

    def __init__(self, templates: list[dict[str, Any]]) -> None:
        self._templates = templates
        self._by_id: dict[str, dict[str, Any]] = {t["id"]: t for t in templates}

    @classmethod
    @lru_cache(maxsize=1)
    def load(cls) -> TemplateLibrary:
        """Load templates from the bundled YAML file. Cached after first call."""
        path = _TEMPLATES_DIR / "goal_nodes.yaml"
        if not path.exists():
            return cls([])
        return cls(_load_yaml(path))

    def all(self) -> list[dict[str, Any]]:
        """Return all templates, ordered as defined in the YAML."""
        return list(self._templates)

    def get(self, template_id: str) -> dict[str, Any] | None:
        """Return a single template by id, or None if not found."""
        return self._by_id.get(template_id)

    def by_domain(self, domain: str) -> list[dict[str, Any]]:
        """Return all templates in a given domain."""
        return [t for t in self._templates if t.get("domain") == domain]

    def domains(self) -> list[str]:
        """Return unique domain names in order of first appearance."""
        seen: set[str] = set()
        out: list[str] = []
        for t in self._templates:
            d = t.get("domain", "")
            if d and d not in seen:
                seen.add(d)
                out.append(d)
        return out
