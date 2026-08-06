"""Deterministic, local preflight checks for the HorizonX CLI."""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from horizonx import Task
from horizonx.storage import SqliteStore

DoctorStatus = Literal["pass", "fail", "info", "unsupported"]

# Doctor is an interactive preflight command, so version probes stay bounded.
_VERSION_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class DoctorCheck:
    """One safe-to-display doctor result."""

    name: str
    status: DoctorStatus
    detail: str
    remedy: str | None = None


@dataclass(frozen=True)
class DoctorReport:
    """The stable, ordered result of local prerequisite checks."""

    checks: tuple[DoctorCheck, ...]

    @property
    def has_failures(self) -> bool:
        return any(check.status in {"fail", "unsupported"} for check in self.checks)

    def lines(self) -> tuple[str, ...]:
        """Render checks without exposing configuration or environment values."""
        return tuple(
            f"{check.name}: {check.detail} [{check.status}]"
            + (f" Remedy: {check.remedy}" if check.remedy else "")
            for check in self.checks
        )


@dataclass(frozen=True)
class _ProviderSpec:
    binary: str | None
    auth_variables: tuple[str, ...]
    install_remedy: str | None


_PROVIDERS: dict[str, _ProviderSpec] = {
    "claude_code": _ProviderSpec(
        binary="claude",
        auth_variables=("ANTHROPIC_API_KEY",),
        install_remedy="Install Claude Code and ensure 'claude' is on PATH.",
    ),
    "codex": _ProviderSpec(
        binary="codex",
        auth_variables=("OPENAI_API_KEY",),
        install_remedy="Install Codex and ensure 'codex' is on PATH.",
    ),
    "openhands": _ProviderSpec(
        binary="openhands",
        auth_variables=("ANTHROPIC_API_KEY", "OPENAI_API_KEY"),
        install_remedy="Install OpenHands and ensure 'openhands' is on PATH.",
    ),
    "custom": _ProviderSpec(binary=None, auth_variables=(), install_remedy=None),
    "mock": _ProviderSpec(binary=None, auth_variables=(), install_remedy=None),
    "sdk": _ProviderSpec(binary=None, auth_variables=(), install_remedy=None),
}

_VERSION_PATTERN = re.compile(r"\b[vV]?(\d+(?:\.\d+){1,3})\b")


def _probe_command_version(binary: str) -> tuple[bool, str]:
    """Return a short version description without inheriting secret-bearing env vars."""
    try:
        result = subprocess.run(
            [binary, "--version"],
            check=False,
            capture_output=True,
            env={"PATH": os.environ.get("PATH", "")},
            text=True,
            timeout=_VERSION_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, "version probe failed"
    if result.returncode != 0:
        return False, "version probe failed"
    match = _VERSION_PATTERN.search(result.stdout)
    if match is None:
        return False, "version could not be safely parsed"
    return True, f"version {match.group(1)}"


def _provider_checks(task: Task | None) -> list[DoctorCheck]:
    selected = task.agent.type if task is not None else None
    provider_names = (selected,) if selected is not None else tuple(_PROVIDERS)
    checks: list[DoctorCheck] = []
    for provider_name in provider_names:
        spec = _PROVIDERS.get(provider_name)
        required = task is not None
        if spec is None:
            checks.append(
                DoctorCheck(
                    name=f"Provider binary: {provider_name}",
                    status="unsupported",
                    detail="provider is not a built-in doctor target",
                    remedy="Install and configure the provider according to its integration documentation.",
                )
            )
            continue
        if provider_name == "openhands" and task is not None:
            mode = str(task.agent.extra.get("mode", "cli"))
            if mode == "server":
                checks.append(
                    DoctorCheck(
                        name="Provider binary: openhands",
                        status="info",
                        detail="server mode; local binary is not required",
                    )
                )
                checks.extend(_provider_auth_checks(provider_name, spec, task, required=False))
                continue
            cli_binary = task.agent.extra.get("cli_bin", "openhands")
            if isinstance(cli_binary, str) and cli_binary.strip():
                spec = _ProviderSpec(
                    binary=cli_binary,
                    auth_variables=spec.auth_variables,
                    install_remedy=spec.install_remedy,
                )
        if spec.binary is None:
            if provider_name == "custom":
                command = _custom_provider_binary(task)
                if command is not None:
                    checks.append(
                        _binary_check(
                            provider_name="custom",
                            binary=command,
                            required=required,
                            remedy="Configure agent.extra.command with a runnable custom provider executable.",
                        )
                    )
                else:
                    checks.append(
                        DoctorCheck(
                            name="Provider binary: custom",
                            status="fail" if required else "info",
                            detail="custom command is not configured",
                            remedy="Set agent.extra.command to the custom provider executable.",
                        )
                    )
            else:
                checks.append(
                    DoctorCheck(
                        name=f"Provider binary: {provider_name}",
                        status="info",
                        detail="no external binary is required",
                    )
                )
        else:
            checks.append(
                _binary_check(
                    provider_name=provider_name,
                    binary=spec.binary,
                    required=required,
                    remedy=spec.install_remedy,
                )
            )

        checks.extend(_provider_auth_checks(provider_name, spec, task, required))
    return checks


def _custom_provider_binary(task: Task | None) -> str | None:
    if task is None:
        return None
    command = task.agent.extra.get("command")
    if isinstance(command, str):
        try:
            arguments = shlex.split(command)
        except ValueError:
            return None
        return arguments[0] if arguments else None
    if isinstance(command, list) and command and isinstance(command[0], str):
        return command[0]
    return None


def _binary_check(
    *, provider_name: str, binary: str, required: bool, remedy: str | None
) -> DoctorCheck:
    location = shutil.which(binary)
    if location is None:
        return DoctorCheck(
            name=f"Provider binary: {provider_name}",
            status="fail" if required else "info",
            detail="not found on PATH",
            remedy=remedy,
        )
    version_ok, version = _probe_command_version(location)
    return DoctorCheck(
        name=f"Provider binary: {provider_name}",
        status="pass" if version_ok else ("fail" if required else "info"),
        detail=version if version_ok else "found, but version probe failed",
        remedy=None if version_ok else "Verify the provider executable can run '--version'.",
    )


def _provider_auth_checks(
    provider_name: str, spec: _ProviderSpec, task: Task | None, required: bool
) -> list[DoctorCheck]:
    if not spec.auth_variables:
        return []
    configured = _auth_is_configured(task, spec.auth_variables)
    remedy = " or ".join(spec.auth_variables)
    if provider_name in {"claude_code", "codex"} and not configured:
        return [
            DoctorCheck(
                name="Provider auth: missing",
                status="info",
                detail=f"for {provider_name}; local CLI login is not verified",
                remedy=(
                    f"Run '{'claude /login' if provider_name == 'claude_code' else 'codex login'}' "
                    f"or configure {remedy} for the task environment."
                ),
            )
        ]
    return [
        DoctorCheck(
            name=f"Provider auth: {'configured' if configured else 'missing'}",
            status="pass" if configured else ("fail" if required else "info"),
            detail=f"for {provider_name}",
            remedy=(
                None
                if configured
                else f"Configure {remedy} for the task or inherited environment."
            ),
        )
    ]


def _auth_is_configured(
    task: Task | None, variables: tuple[str, ...]
) -> bool:
    if task is None:
        return any(bool(os.environ.get(variable)) for variable in variables)
    environment = task.environment
    return any(
        environment.env.get(variable)
        or (variable in environment.inherit_env and bool(os.environ.get(variable)))
        for variable in variables
    )


def _git_check() -> DoctorCheck:
    location = shutil.which("git")
    if location is None:
        return DoctorCheck(
            name="Git",
            status="fail",
            detail="not found on PATH",
            remedy="Install Git and ensure 'git' is on PATH.",
        )
    version_ok, version = _probe_command_version(location)
    return DoctorCheck(
        name="Git",
        status="pass" if version_ok else "fail",
        detail=version if version_ok else "found, but version probe failed",
        remedy=None if version_ok else "Verify the Git executable can run '--version'.",
    )


def _backend_check(task: Task | None) -> DoctorCheck:
    backend = task.environment.type if task is not None else "local"
    if backend == "local":
        return DoctorCheck(
            name="Workspace backend",
            status="pass",
            detail="local is explicitly supported",
        )
    return DoctorCheck(
        name="Workspace backend",
        status="unsupported",
        detail=f"{backend} is not supported and was not probed",
        remedy="Use environment.type: local.",
    )


def _writable_path_check(name: str, path: Path) -> DoctorCheck:
    directory = path if name == "Workspace root" else path.parent
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".horizonx-doctor-", dir=directory, delete=True):
            pass
    except OSError:
        return DoctorCheck(
            name=name,
            status="fail",
            detail=f"{path} is not writable",
            remedy=f"Grant write access to {directory} or choose a different path.",
        )
    return DoctorCheck(name=name, status="pass", detail=f"{path} is writable")


def _port_check(port: int | None) -> DoctorCheck | None:
    if port is None:
        return None
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            return DoctorCheck(
                name=f"Loopback port: {port}",
                status="fail",
                detail="already in use or unavailable",
                remedy="Choose another port or stop the process using this port.",
            )
    return DoctorCheck(
        name=f"Loopback port: {port}", status="pass", detail="available on 127.0.0.1"
    )


async def _sqlite_checks(db_path: Path) -> list[DoctorCheck]:
    try:
        store = SqliteStore(db_path)
    except Exception:
        return [
            DoctorCheck(
                name="SQLite database",
                status="fail",
                detail="file-backed local database policy could not be established",
                remedy="Use a writable SQLite file on a supported local filesystem.",
            )
        ]
    try:
        version, integrity, settings = await asyncio.gather(
            store.schema_version(), store.integrity_check(), store.connection_settings()
        )
    except Exception:
        return [
            DoctorCheck(
                name="SQLite database",
                status="fail",
                detail="schema or integrity check could not complete",
                remedy="Check database permissions and restore from a verified backup if needed.",
            )
        ]
    finally:
        await store.close()

    integrity_ok = integrity == ["ok"]
    policy_ok = settings["journal_mode"].lower() == "wal" and bool(settings["foreign_keys"])
    return [
        DoctorCheck(name="Database path", status="pass", detail=str(db_path)),
        DoctorCheck(name="Schema version", status="pass", detail=str(version)),
        DoctorCheck(
            name="Integrity",
            status="pass" if integrity_ok else "fail",
            detail="ok" if integrity_ok else "integrity check reported an error",
            remedy=None if integrity_ok else "Restore the database from a verified backup.",
        ),
        DoctorCheck(
            name="Connection policy",
            status="pass" if policy_ok else "fail",
            detail=(
                f"WAL={settings['journal_mode'].lower() == 'wal'}, "
                f"foreign keys={bool(settings['foreign_keys'])}, "
                f"busy timeout={settings['busy_timeout']}ms"
            ),
            remedy=None if policy_ok else "Use HorizonX's SQLite store policy for this database.",
        ),
    ]


async def run_doctor(
    *, db_path: Path, workspace_root: Path, task: Task | None = None, port: int | None = None
) -> DoctorReport:
    """Run all local, bounded preflight checks in a stable display order."""
    checks = [*_provider_checks(task), _git_check(), _backend_check(task)]
    checks.extend(
        [
            _writable_path_check("Database path", db_path),
            _writable_path_check("Workspace root", workspace_root),
        ]
    )
    port_result = _port_check(port)
    if port_result is not None:
        checks.append(port_result)
    checks.extend(await _sqlite_checks(db_path))
    return DoctorReport(checks=tuple(checks))
