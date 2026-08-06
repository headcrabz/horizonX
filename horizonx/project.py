"""Project-level configuration for the HorizonX command-line interface."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator

CONFIG_FILENAME = "horizonx.yaml"


class ProjectConfig(BaseModel):
    """Validated paths shared by commands run from a project directory."""

    model_config = ConfigDict(extra="forbid", validate_default=True)

    version: Literal[1] = 1
    db_path: Path = Path("horizonx.db")
    workspace_root: Path = Path("horizonx-workspaces")
    generated_state_paths: bool = False

    @field_validator("db_path", mode="before")
    @classmethod
    def _db_path_must_not_be_blank(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("db_path must not be blank")
        return value

    @field_validator("db_path", "workspace_root", mode="after")
    @classmethod
    def _resolve_configured_path(cls, value: Path, info: ValidationInfo) -> Path:
        context = info.context or {}
        config_directory = context.get("config_directory")
        if config_directory is None:
            return value
        resolved = _resolve_path(value, Path(config_directory))
        if info.field_name == "db_path" and resolved.is_dir():
            raise ValueError("db_path must name a database file, not a directory")
        return resolved

    @classmethod
    def load(cls, path: Path) -> ProjectConfig:
        """Load a config file and resolve its paths relative to that file."""
        data = yaml.safe_load(path.read_text())
        return cls.model_validate(
            data, context={"config_directory": path.parent.resolve()}
        )

    @classmethod
    def find_in(cls, directory: Path) -> ProjectConfig | None:
        """Load this directory's config if it exists."""
        path = directory / CONFIG_FILENAME
        return cls.load(path) if path.exists() else None

    def to_yaml(self) -> str:
        """Serialize the portable defaults used by ``horizonx init``."""
        return yaml.safe_dump(
            self.model_dump(mode="json"), sort_keys=False, default_flow_style=False
        )


def _resolve_path(path: Path, directory: Path) -> Path:
    path = path.expanduser()
    return path.resolve() if path.is_absolute() else (directory / path).resolve()
