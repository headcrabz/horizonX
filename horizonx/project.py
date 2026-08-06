"""Project-level configuration for the HorizonX command-line interface."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

CONFIG_FILENAME = "horizonx.yaml"


class ProjectConfig(BaseModel):
    """Validated paths shared by commands run from a project directory."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    db_path: Path = Path("horizonx.db")
    workspace_root: Path = Path("horizonx-workspaces")

    @classmethod
    def load(cls, path: Path) -> ProjectConfig:
        """Load a config file and resolve its paths relative to that file."""
        data = yaml.safe_load(path.read_text())
        config = cls.model_validate(data)
        directory = path.parent.resolve()
        return config.model_copy(
            update={
                "db_path": _resolve_path(config.db_path, directory),
                "workspace_root": _resolve_path(config.workspace_root, directory),
            }
        )

    @classmethod
    def find_in(cls, directory: Path) -> ProjectConfig | None:
        """Load this directory's config if it exists."""
        path = directory / CONFIG_FILENAME
        return cls.load(path) if path.is_file() else None

    def to_yaml(self) -> str:
        """Serialize the portable defaults used by ``horizonx init``."""
        return yaml.safe_dump(
            self.model_dump(mode="json"), sort_keys=False, default_flow_style=False
        )


def _resolve_path(path: Path, directory: Path) -> Path:
    path = path.expanduser()
    return path.resolve() if path.is_absolute() else (directory / path).resolve()
