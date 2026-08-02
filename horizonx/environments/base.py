"""Execution-environment contracts and typed preparation failures."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from horizonx.core.types import RepositoryConfig


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    elapsed: float


@dataclass(frozen=True)
class WorkspaceMetadata:
    run_id: str
    backend: str
    source_kind: str
    source: str | None
    source_commit: str | None
    head_commit: str | None
    branch: str | None
    created_at: str
    setup_complete: bool

    def to_dict(self) -> dict[str, str | bool | None]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> WorkspaceMetadata:
        return cls(
            run_id=str(value["run_id"]),
            backend=str(value["backend"]),
            source_kind=str(value["source_kind"]),
            source=str(value["source"]) if value.get("source") is not None else None,
            source_commit=(
                str(value["source_commit"])
                if value.get("source_commit") is not None
                else None
            ),
            head_commit=(
                str(value["head_commit"])
                if value.get("head_commit") is not None
                else (
                    str(value["source_commit"])
                    if value.get("source_commit") is not None
                    else None
                )
            ),
            branch=str(value["branch"]) if value.get("branch") is not None else None,
            created_at=str(value["created_at"]),
            setup_complete=bool(value["setup_complete"]),
        )


@dataclass(frozen=True)
class PreparedWorkspace:
    path: Path
    env: dict[str, str]
    metadata: WorkspaceMetadata


@dataclass(frozen=True)
class BackendHealth:
    healthy: bool
    git_version: str
    detail: str = ""


class WorkspaceError(RuntimeError):
    """Base class for workspace lifecycle failures."""


class WorkspaceContainmentError(WorkspaceError):
    """Raised when a workspace escapes the configured managed root."""


class WorkspacePreparationError(WorkspaceError):
    """Raised when a repository cannot be prepared safely."""


class SetupCommandError(WorkspacePreparationError):
    """Raised when a configured setup command fails."""

    def __init__(self, command: str, result: CommandResult):
        self.command = command
        self.result = result
        super().__init__(
            f"setup command failed with exit {result.returncode}: {command}"
        )


class EnvironmentBackend(Protocol):
    async def prepare(
        self, run_id: str, repository: RepositoryConfig | None
    ) -> PreparedWorkspace: ...

    async def resume(self, workspace_path: Path) -> PreparedWorkspace: ...

    async def execute(
        self, workspace_path: Path, command: str, *, timeout: float
    ) -> CommandResult: ...

    async def snapshot(self, workspace_path: Path) -> WorkspaceMetadata: ...

    async def cleanup(self, workspace_path: Path, *, preserve: bool = True) -> None: ...

    async def health(self) -> BackendHealth: ...
