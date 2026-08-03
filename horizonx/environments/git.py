"""Contained local repository materialization using Git worktrees."""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from horizonx.core.types import EnvironmentConfig, RepositoryConfig
from horizonx.environments.base import (
    BackendHealth,
    CommandResult,
    PreparedWorkspace,
    SetupCommandError,
    WorkspaceContainmentError,
    WorkspaceMetadata,
    WorkspacePreparationError,
)
from horizonx.security.environment_policy import redact_secrets
from horizonx.security.process import (
    collect_process_output,
    spawn_process,
    spawn_shell,
)

_RUN_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_METADATA_PATH = Path(".horizonx/workspace.json")


class GitWorktreeBackend:
    """Prepare one deterministic, contained workspace for a local run."""

    def __init__(self, workspace_root: Path, config: EnvironmentConfig):
        self.workspace_root = workspace_root.expanduser().resolve()
        self.config = config
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    async def prepare(
        self, run_id: str, repository: RepositoryConfig | None
    ) -> PreparedWorkspace:
        if not _RUN_ID.fullmatch(run_id):
            raise WorkspaceContainmentError(f"unsafe run id for workspace path: {run_id!r}")
        target = self._contained(self.workspace_root / run_id)
        if target.exists():
            raise WorkspacePreparationError(
                f"workspace already exists and will not be reset: {target}"
            )

        if repository is None:
            target.mkdir(parents=True)
            metadata = self._metadata(
                run_id=run_id,
                source_kind="empty",
                source=None,
                source_commit=None,
                head_commit=None,
                branch=None,
                setup_complete=False,
            )
        elif repository.path is not None:
            metadata = await self._prepare_local_worktree(run_id, target, repository)
        else:
            metadata = await self._prepare_clone(run_id, target, repository)

        self._write_metadata(target, metadata)
        try:
            await self._run_setup(target)
        except SetupCommandError:
            self._write_metadata(target, metadata)
            raise
        metadata = replace(metadata, setup_complete=True)
        self._write_metadata(target, metadata)
        return PreparedWorkspace(target, self._effective_env(target), metadata)

    async def resume(self, workspace_path: Path) -> PreparedWorkspace:
        path = self._contained(workspace_path)
        metadata = self._read_metadata(path)
        if metadata.backend != "local_git_worktree":
            raise WorkspacePreparationError(
                f"workspace backend is not supported: {metadata.backend}"
            )
        if not metadata.setup_complete:
            raise WorkspacePreparationError(
                f"workspace setup did not complete and requires explicit recovery: {path}"
            )
        return PreparedWorkspace(path, self._effective_env(path), metadata)

    async def execute(
        self, workspace_path: Path, command: str, *, timeout: float
    ) -> CommandResult:
        path = self._contained(workspace_path)
        start = time.monotonic()
        proc = await spawn_shell(
            command, cwd=path, env=self._effective_env(path)
        )
        stdout, stderr, timed_out = await collect_process_output(
            proc, timeout=timeout
        )
        if timed_out:
            return CommandResult(-1, "", "timeout", time.monotonic() - start)
        return CommandResult(
            proc.returncode or 0,
            str(
                redact_secrets(
                    (stdout or b"").decode(errors="replace"),
                    self._effective_env(path),
                )
            ),
            str(
                redact_secrets(
                    (stderr or b"").decode(errors="replace"),
                    self._effective_env(path),
                )
            ),
            time.monotonic() - start,
        )

    async def snapshot(self, workspace_path: Path) -> WorkspaceMetadata:
        path = self._contained(workspace_path)
        metadata = self._read_metadata(path)
        if (path / ".git").exists():
            source_commit = await self._git_output(path, "rev-parse", "HEAD")
            branch_value = await self._git_output(
                path, "rev-parse", "--abbrev-ref", "HEAD"
            )
            metadata = replace(
                metadata,
                head_commit=source_commit,
                branch=None if branch_value == "HEAD" else branch_value,
            )
            self._write_metadata(path, metadata)
        return metadata

    async def cleanup(self, workspace_path: Path, *, preserve: bool = True) -> None:
        path = self._contained(workspace_path)
        if preserve:
            return
        metadata = self._read_metadata(path)
        if metadata.source_kind == "local_path" and metadata.source:
            await self._git(Path(metadata.source), "worktree", "remove", str(path))
        else:
            shutil.rmtree(path)

    async def health(self) -> BackendHealth:
        try:
            result = await self._exec("git", "--version")
        except FileNotFoundError:
            return BackendHealth(False, "", "git executable not found")
        return BackendHealth(
            result.returncode == 0,
            result.stdout.strip(),
            result.stderr.strip(),
        )

    async def _prepare_local_worktree(
        self, run_id: str, target: Path, repository: RepositoryConfig
    ) -> WorkspaceMetadata:
        assert repository.path is not None
        source = repository.path.expanduser().resolve()
        if not source.is_dir():
            raise WorkspacePreparationError(f"repository path is not a directory: {source}")
        repo_root = Path(await self._git_output(source, "rev-parse", "--show-toplevel"))
        try:
            self.workspace_root.relative_to(repo_root)
        except ValueError:
            pass
        else:
            raise WorkspaceContainmentError(
                "workspace root must be outside the source repository to keep the "
                f"source tree unchanged: {self.workspace_root}"
            )
        commit = await self._git_output(repo_root, "rev-parse", f"{repository.ref}^{{commit}}")
        args = ["worktree", "add"]
        if repository.branch:
            args.extend(["-b", repository.branch])
        else:
            args.append("--detach")
        args.extend([str(target), commit])
        await self._git(repo_root, *args)
        if repository.submodules:
            await self._git(target, "submodule", "update", "--init", "--recursive")
        branch = await self._branch(target)
        return self._metadata(
            run_id=run_id,
            source_kind="local_path",
            source=str(repo_root),
            source_commit=commit,
            head_commit=commit,
            branch=branch,
            setup_complete=False,
        )

    async def _prepare_clone(
        self, run_id: str, target: Path, repository: RepositoryConfig
    ) -> WorkspaceMetadata:
        if not repository.url:
            raise WorkspacePreparationError("clone URL is required")
        await self._exec_checked("git", "clone", "--no-checkout", repository.url, str(target))
        commit = await self._git_output(target, "rev-parse", f"{repository.ref}^{{commit}}")
        if repository.branch:
            await self._git(target, "switch", "-c", repository.branch, commit)
        else:
            await self._git(target, "checkout", "--detach", commit)
        if repository.submodules:
            await self._git(target, "submodule", "update", "--init", "--recursive")
        return self._metadata(
            run_id=run_id,
            source_kind="clone_url",
            source=self._redact(repository.url),
            source_commit=commit,
            head_commit=commit,
            branch=await self._branch(target),
            setup_complete=False,
        )

    async def _run_setup(self, target: Path) -> None:
        for command in self.config.setup_commands:
            result = await self.execute(
                target, command, timeout=self.config.setup_timeout_seconds
            )
            if result.returncode != 0:
                raise SetupCommandError(command, result)

    def _effective_env(self, workspace: Path | None = None) -> dict[str, str]:
        inherited = {
            name: os.environ[name]
            for name in self.config.inherit_env
            if name in os.environ
        }
        effective = {**inherited, **self.config.env}
        if workspace is not None:
            venv_bin = workspace / ".venv" / "bin"
            if venv_bin.is_dir():
                current_path = effective.get("PATH", "")
                effective["PATH"] = (
                    f"{venv_bin}{os.pathsep}{current_path}"
                    if current_path
                    else str(venv_bin)
                )
                effective["VIRTUAL_ENV"] = str(workspace / ".venv")
        return effective

    def _contained(self, path: Path) -> Path:
        candidate = path.expanduser().resolve()
        try:
            relative = candidate.relative_to(self.workspace_root)
        except ValueError as exc:
            raise WorkspaceContainmentError(
                f"workspace is outside managed root {self.workspace_root}: {candidate}"
            ) from exc
        if relative == Path("."):
            raise WorkspaceContainmentError("workspace cannot be the managed root itself")
        return candidate

    def _metadata(
        self,
        *,
        run_id: str,
        source_kind: str,
        source: str | None,
        source_commit: str | None,
        head_commit: str | None,
        branch: str | None,
        setup_complete: bool,
    ) -> WorkspaceMetadata:
        return WorkspaceMetadata(
            run_id=run_id,
            backend="local_git_worktree",
            source_kind=source_kind,
            source=source,
            source_commit=source_commit,
            head_commit=head_commit,
            branch=branch,
            created_at=datetime.now(UTC).isoformat(),
            setup_complete=setup_complete,
        )

    def _write_metadata(self, workspace: Path, metadata: WorkspaceMetadata) -> None:
        destination = workspace / _METADATA_PATH
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(json.dumps(metadata.to_dict(), indent=2, sort_keys=True))
        temporary.replace(destination)

    def _read_metadata(self, workspace: Path) -> WorkspaceMetadata:
        destination = workspace / _METADATA_PATH
        if not destination.is_file():
            raise WorkspacePreparationError(
                f"workspace metadata is missing; refusing implicit recovery: {workspace}"
            )
        try:
            value = json.loads(destination.read_text())
            if not isinstance(value, dict):
                raise TypeError("metadata root must be an object")
            return WorkspaceMetadata.from_dict(value)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise WorkspacePreparationError(
                f"workspace metadata is invalid: {destination}"
            ) from exc

    async def _branch(self, repo: Path) -> str | None:
        value = await self._git_output(repo, "rev-parse", "--abbrev-ref", "HEAD")
        return None if value == "HEAD" else value

    async def _git(self, repo: Path, *args: str) -> CommandResult:
        return await self._exec_checked("git", "-C", str(repo), *args)

    async def _git_output(self, repo: Path, *args: str) -> str:
        return (await self._git(repo, *args)).stdout.strip()

    async def _exec_checked(self, *args: str) -> CommandResult:
        result = await self._exec(*args)
        if result.returncode != 0:
            rendered = " ".join(self._redact(arg) for arg in args)
            raise WorkspacePreparationError(
                f"command failed ({result.returncode}): {rendered}\n"
                f"{self._redact(result.stderr.strip())}"
            )
        return result

    def _redact(self, value: str) -> str:
        uri_redacted = re.sub(r"(://)[^/@]+@", r"\1<redacted>@", value)
        return str(redact_secrets(uri_redacted, self._effective_env()))

    async def _exec(self, *args: str) -> CommandResult:
        start = time.monotonic()
        proc = await spawn_process(
            *args, cwd=self.workspace_root, env=self._effective_env()
        )
        stdout, stderr, _ = await collect_process_output(proc)
        return CommandResult(
            proc.returncode or 0,
            (stdout or b"").decode(errors="replace"),
            (stderr or b"").decode(errors="replace"),
            time.monotonic() - start,
        )
