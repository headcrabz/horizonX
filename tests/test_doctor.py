"""Coverage for the preflight doctor command."""

from __future__ import annotations

import io
import shutil
import signal
import socket
import subprocess
from pathlib import Path

from click.testing import CliRunner

from horizonx.cli import main
from horizonx.doctor import (
    _PROBE_DRAIN_JOIN_TIMEOUT_SECONDS,
    _PROBE_STREAM_LIMIT_BYTES,
    DoctorCheck,
    DoctorReport,
    _drain_probe_stream,
    _join_probe_drainers,
    _probe_command_version,
)


class _VersionProcess:
    pid = 1

    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = io.BytesIO(stdout.encode())
        self.stderr = io.BytesIO()
        self.returncode = returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode


def _task(path: Path, *, provider: str = "codex") -> Path:
    path.write_text(
        "\n".join(
            [
                "id: doctor-task",
                "name: Doctor task",
                "prompt: check prerequisites",
                "strategy: {kind: single}",
                f"agent: {{type: {provider}, model: test-model}}",
            ]
        )
    )
    return path


def test_doctor_uses_task_provider_and_reports_missing_binary_remedy(
    tmp_path: Path, monkeypatch
) -> None:
    task_path = _task(tmp_path / "task.yaml", provider="codex")
    monkeypatch.setattr("horizonx.doctor.shutil.which", lambda _: None)

    result = CliRunner().invoke(
        main, ["--db", str(tmp_path / "runs.db"), "doctor", "--task", str(task_path)]
    )

    assert result.exit_code != 0
    assert "provider binary: codex" in result.output.lower()
    assert "install codex" in result.output.lower()
    assert "claude_code" not in result.output.lower()


def test_doctor_reports_busy_requested_port(tmp_path: Path) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    try:
        port = listener.getsockname()[1]
        result = CliRunner().invoke(
            main, ["--db", str(tmp_path / "runs.db"), "doctor", "--port", str(port)]
        )
    finally:
        listener.close()

    assert result.exit_code != 0
    assert f"loopback port: {port}" in result.output.lower()
    assert "choose another port" in result.output.lower()


def test_doctor_uses_project_database_and_workspace_paths(tmp_path: Path, monkeypatch) -> None:
    configured_db = tmp_path / "data" / "project.db"
    configured_workspace = tmp_path / "work" / "spaces"
    (tmp_path / "horizonx.yaml").write_text(
        f"version: 1\ndb_path: {configured_db.relative_to(tmp_path)}\n"
        f"workspace_root: {configured_workspace.relative_to(tmp_path)}\n"
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["doctor"])

    assert result.exit_code == 0, result.output
    assert str(configured_db) in result.output
    assert str(configured_workspace) in result.output
    assert configured_db.is_file()
    assert not list(configured_db.parent.glob(".horizonx-doctor-*"))
    assert not list(configured_workspace.parent.glob(".horizonx-doctor-*"))


def test_doctor_never_emits_secret_environment_values(tmp_path: Path, monkeypatch) -> None:
    secret = "doctor-secret-value-must-not-appear"
    task_path = _task(tmp_path / "task.yaml", provider="codex")
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    monkeypatch.setattr("horizonx.doctor.shutil.which", lambda _: None)

    result = CliRunner().invoke(
        main, ["--db", str(tmp_path / "runs.db"), "doctor", "--task", str(task_path)]
    )

    assert secret not in result.output
    assert "auth: configured" in result.output.lower()


def test_doctor_constrains_untrusted_provider_version_output(
    tmp_path: Path, monkeypatch
) -> None:
    secret = "provider-version-output-secret"
    task_path = _task(tmp_path / "task.yaml", provider="codex")
    monkeypatch.setenv("OPENAI_API_KEY", "configured")
    monkeypatch.setattr("horizonx.doctor.shutil.which", lambda _: "/pretend/codex")
    monkeypatch.setattr(
        "horizonx.doctor._start_probe_process",
        lambda _: _VersionProcess(f"Codex 9.8.7 {secret}"),
    )

    result = CliRunner().invoke(
        main, ["--db", str(tmp_path / "runs.db"), "doctor", "--task", str(task_path)]
    )

    assert result.exit_code == 0, result.output
    assert "version 9.8.7" in result.output.lower()
    assert secret not in result.output


def test_doctor_does_not_require_openhands_binary_in_server_mode(
    tmp_path: Path, monkeypatch
) -> None:
    task_path = _task(tmp_path / "task.yaml", provider="openhands")
    task_path.write_text(task_path.read_text() + "\nagent:\n  type: openhands\n  model: test-model\n  extra:\n    mode: server\n")
    original_which = shutil.which
    monkeypatch.setattr(
        "horizonx.doctor.shutil.which",
        lambda binary: None if binary == "openhands" else original_which(binary),
    )

    result = CliRunner().invoke(
        main, ["--db", str(tmp_path / "runs.db"), "doctor", "--task", str(task_path)]
    )

    assert result.exit_code == 0, result.output
    assert "server mode; local binary is not required" in result.output.lower()


def test_doctor_checks_the_configured_custom_provider_binary(
    tmp_path: Path, monkeypatch
) -> None:
    task_path = tmp_path / "task.yaml"
    task_path.write_text(
        "\n".join(
            [
                "id: custom-doctor-task",
                "name: Custom doctor task",
                "prompt: check prerequisites",
                "strategy: {kind: single}",
                "agent:",
                "  type: custom",
                "  model: test-model",
                "  extra: {command: custom-provider}",
            ]
        )
    )
    original_which = shutil.which
    monkeypatch.setattr(
        "horizonx.doctor.shutil.which",
        lambda binary: None if binary == "custom-provider" else original_which(binary),
    )

    result = CliRunner().invoke(
        main, ["--db", str(tmp_path / "runs.db"), "doctor", "--task", str(task_path)]
    )

    assert result.exit_code != 0
    assert "provider binary: custom" in result.output.lower()
    assert "custom provider executable" in result.output.lower()


def test_doctor_treats_missing_claude_api_key_as_local_login_information(
    tmp_path: Path, monkeypatch
) -> None:
    task_path = _task(tmp_path / "task.yaml", provider="claude_code")
    original_which = shutil.which
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("horizonx.doctor.Path.home", lambda: tmp_path / "empty-home")
    monkeypatch.setattr(
        "horizonx.doctor.shutil.which",
        lambda binary: "/pretend/claude" if binary == "claude" else original_which(binary),
    )
    monkeypatch.setattr(
        "horizonx.doctor._start_probe_process",
        lambda _: _VersionProcess("Claude 2.1.0"),
    )

    result = CliRunner().invoke(
        main, ["--db", str(tmp_path / "runs.db"), "doctor", "--task", str(task_path)]
    )

    assert result.exit_code == 0, result.output
    assert "local cli login is not verified" in result.output.lower()
    assert "[info]" in result.output.lower()


def test_doctor_treats_missing_codex_api_key_as_local_login_information(
    tmp_path: Path, monkeypatch
) -> None:
    task_path = _task(tmp_path / "task.yaml", provider="codex")
    original_which = shutil.which
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        "horizonx.doctor.shutil.which",
        lambda binary: "/pretend/codex" if binary == "codex" else original_which(binary),
    )
    monkeypatch.setattr(
        "horizonx.doctor._start_probe_process",
        lambda _: _VersionProcess("Codex 1.2.3"),
    )

    result = CliRunner().invoke(
        main, ["--db", str(tmp_path / "runs.db"), "doctor", "--task", str(task_path)]
    )

    assert result.exit_code == 0, result.output
    assert "local cli login is not verified" in result.output.lower()
    assert "[info]" in result.output.lower()


def test_version_probe_times_out_with_a_bounded_safe_failure(monkeypatch) -> None:
    class TimedOutProcess:
        pid = 123
        stdout = io.BytesIO()
        stderr = io.BytesIO()

        def wait(self, timeout: float | None = None) -> int:
            raise subprocess.TimeoutExpired("provider", timeout)

    monkeypatch.setattr("horizonx.doctor._start_probe_process", lambda _: TimedOutProcess())
    monkeypatch.setattr("horizonx.doctor.os.killpg", lambda *args, **kwargs: None)

    successful, detail = _probe_command_version("provider")

    assert not successful
    assert detail == "version probe failed"


def test_version_probe_retains_only_a_bounded_tail_when_output_floods() -> None:
    payload = b"x" * (_PROBE_STREAM_LIMIT_BYTES * 2) + b"tail"

    retained = _drain_probe_stream(io.BytesIO(payload))

    assert retained == b"x" * (_PROBE_STREAM_LIMIT_BYTES - 4) + b"tail"


def test_timed_out_version_probe_closes_pipes_and_bounds_drain_joins() -> None:
    calls: list[float | None] = []

    class BlockingDrain:
        def join(self, timeout: float | None = None) -> None:
            calls.append(timeout)

    class ProbePipe:
        closed = False

        def close(self) -> None:
            self.closed = True

    stdout = ProbePipe()
    stderr = ProbePipe()

    _join_probe_drainers((BlockingDrain(), BlockingDrain()), (stdout, stderr), timed_out=True)

    assert stdout.closed and stderr.closed
    assert calls == [_PROBE_DRAIN_JOIN_TIMEOUT_SECONDS] * 2


def test_version_probe_terminates_the_owned_process_group_on_timeout(monkeypatch) -> None:
    calls: list[tuple[int, int]] = []

    class TimedOutProcess:
        pid = 456
        stdout = io.BytesIO()
        stderr = io.BytesIO()

        def __init__(self) -> None:
            self.wait_calls = 0

        def wait(self, timeout: float | None = None) -> int:
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise subprocess.TimeoutExpired("provider", timeout)
            return 1

    def killpg(pid: int, sig: int) -> None:
        calls.append((pid, sig))
        if sig == 0:
            raise ProcessLookupError

    monkeypatch.setattr("horizonx.doctor._start_probe_process", lambda _: TimedOutProcess())
    monkeypatch.setattr("horizonx.doctor.os.killpg", killpg)

    successful, detail = _probe_command_version("provider")

    assert not successful
    assert detail == "version probe failed"
    assert calls == [(456, signal.SIGTERM), (456, 0)]


def test_doctor_rejects_unloadable_task_with_an_actionable_error(tmp_path: Path) -> None:
    missing = tmp_path / "missing-task.yaml"

    result = CliRunner().invoke(main, ["doctor", "--task", str(missing)])

    assert result.exit_code != 0
    assert "could not load task" in result.output.lower()
    assert "check the path" in result.output.lower()


def test_doctor_rejects_a_non_utf8_task_with_an_actionable_error(tmp_path: Path) -> None:
    task_path = tmp_path / "task.yaml"
    task_path.write_bytes(b"\xff\xfe")

    result = CliRunner().invoke(main, ["doctor", "--task", str(task_path)])

    assert result.exit_code != 0
    assert "could not load task" in result.output.lower()
    assert "check the path" in result.output.lower()


def test_doctor_names_an_unsupported_task_backend_without_echoing_task_data(
    tmp_path: Path,
) -> None:
    task_path = _task(tmp_path / "task.yaml")
    task_path.write_text(task_path.read_text() + "\nenvironment: {type: container}\n")

    result = CliRunner().invoke(main, ["doctor", "--task", str(task_path)])

    assert result.exit_code != 0
    assert "unsupported workspace backend: container" in result.output.lower()
    assert "environment.type: local" in result.output.lower()


def test_doctor_report_treats_an_unsupported_required_boundary_as_failure() -> None:
    report = DoctorReport(
        checks=(
            DoctorCheck(
                name="Workspace backend",
                status="unsupported",
                detail="container is not supported",
            ),
        )
    )

    assert report.has_failures
