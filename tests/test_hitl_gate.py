"""Tests for HITL gate — Slack notification, webhook retry, timeout escalation."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from horizonx.core.types import (
    AgentConfig,
    HITLConfig,
    HITLDecision,
    Run,
    RunStatus,
    StrategyConfig,
    Task,
)
from horizonx.hitl.gate import _notify_slack, _notify_webhook, await_decision

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run(tmp_path: Path) -> Run:
    task = Task(
        id="hitl-test-task",
        name="HITL Test",
        description="test",
        prompt="do stuff",
        strategy=StrategyConfig(kind="single"),
        agent=AgentConfig(type="mock", model="mock"),
    )
    ws = tmp_path / "ws-hitl"
    ws.mkdir(exist_ok=True)
    return Run(
        id="run-hitl-001",
        task=task,
        workspace_path=ws,
        status=RunStatus.PAUSED_HITL,
    )


# ---------------------------------------------------------------------------
# _notify_slack
# ---------------------------------------------------------------------------


async def test_notify_slack_no_token(tmp_path: Path) -> None:
    """No token set — function returns silently without calling slack_sdk."""
    cfg = HITLConfig(notification_type="slack", notification_target="#ops")
    with patch.dict("os.environ", {}, clear=False):
        import os
        os.environ.pop("HORIZONX_SLACK_TOKEN", None)
        # Should not raise
        await _notify_slack("#ops", "run-1", "test reason", {}, cfg)


async def test_notify_slack_no_channel(tmp_path: Path) -> None:
    """No channel — function returns silently."""
    cfg = HITLConfig(notification_type="slack")
    with patch.dict("os.environ", {"HORIZONX_SLACK_TOKEN": "xoxb-fake"}):
        await _notify_slack(None, "run-1", "test reason", {}, cfg)


async def test_notify_slack_sends_block_kit(tmp_path: Path) -> None:
    """When token + channel present, sends a Block Kit message via AsyncWebClient."""
    import sys

    cfg = HITLConfig(notification_type="slack", notification_target="#ops")

    mock_client = AsyncMock()
    mock_client.chat_postMessage = AsyncMock(return_value={"ok": True})

    # Build a fake slack_sdk module tree so the import inside _notify_slack succeeds
    mock_async_client_module = MagicMock()
    mock_async_client_module.AsyncWebClient = MagicMock(return_value=mock_client)

    fake_modules = {
        "slack_sdk": MagicMock(),
        "slack_sdk.web": MagicMock(),
        "slack_sdk.web.async_client": mock_async_client_module,
    }

    with patch.dict("os.environ", {"HORIZONX_SLACK_TOKEN": "xoxb-fake"}):
        with patch.dict(sys.modules, fake_modules):
            await _notify_slack("#ops", "run-abc", "validator failed", {"k": "v"}, cfg)

    mock_client.chat_postMessage.assert_called_once()
    call_kwargs = mock_client.chat_postMessage.call_args.kwargs
    assert call_kwargs["channel"] == "#ops"
    assert "run-abc" in call_kwargs["text"] or "run-abc" in str(call_kwargs["blocks"])


async def test_notify_slack_import_error_writes_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """If slack_sdk is not installed, writes helpful message to stderr."""
    cfg = HITLConfig(notification_type="slack", notification_target="#ops")

    with patch.dict("os.environ", {"HORIZONX_SLACK_TOKEN": "xoxb-fake"}):
        with patch.dict("sys.modules", {"slack_sdk.web.async_client": None}):
            import sys
            # Simulate ImportError by patching the import path used in gate.py
            original = sys.modules.get("slack_sdk")  # noqa: F841
            sys.modules.pop("slack_sdk", None)
            sys.modules.pop("slack_sdk.web", None)
            sys.modules.pop("slack_sdk.web.async_client", None)

            # We need to actually trigger ImportError in the gate module
            # by making the import fail inside the try block
            import builtins
            real_import = builtins.__import__

            def mock_import(name: str, *args: Any, **kwargs: Any) -> Any:
                if "slack_sdk" in name:
                    raise ImportError("No module named 'slack_sdk'")
                return real_import(name, *args, **kwargs)

            with patch.object(builtins, "__import__", side_effect=mock_import):
                import sys as _sys
                captured_stderr = []
                original_write = _sys.stderr.write

                def capture_write(s: str) -> None:
                    captured_stderr.append(s)

                _sys.stderr.write = capture_write  # type: ignore[method-assign]
                try:
                    await _notify_slack("#ops", "run-1", "reason", {}, cfg)
                finally:
                    _sys.stderr.write = original_write  # type: ignore[method-assign]

            assert any("slack_sdk not installed" in s for s in captured_stderr)


# ---------------------------------------------------------------------------
# _notify_webhook
# ---------------------------------------------------------------------------


async def test_notify_webhook_no_url() -> None:
    """No URL — returns silently."""
    await _notify_webhook(None, "run-1", "reason", {})


async def test_notify_webhook_success_first_attempt(tmp_path: Path) -> None:
    """Succeeds on first attempt with 2xx response."""
    mock_response = MagicMock()
    mock_response.status_code = 200

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    call_count = 0

    async def mock_post(url: str, json: Any) -> MagicMock:
        nonlocal call_count
        call_count += 1
        return mock_response

    mock_client.post = mock_post

    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        await _notify_webhook("https://example.com/hook", "run-1", "reason", {"k": "v"})

    assert call_count == 1


async def test_notify_webhook_retries_on_5xx(tmp_path: Path) -> None:
    """Retries up to 3 times on 5xx, then writes failure to stderr."""
    import sys
    stderr_messages: list[str] = []
    original_write = sys.stderr.write

    def capture(s: str) -> None:
        stderr_messages.append(s)

    sys.stderr.write = capture  # type: ignore[method-assign]

    try:
        attempt_count = 0

        async def mock_post(url: str, json: Any) -> MagicMock:
            nonlocal attempt_count
            attempt_count += 1
            r = MagicMock()
            r.status_code = 503
            return r

        mock_client = MagicMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            # Override asyncio.sleep to avoid actual waiting
            with patch("asyncio.sleep", new_callable=AsyncMock):
                await _notify_webhook("https://example.com/hook", "run-1", "reason", {})
    finally:
        sys.stderr.write = original_write  # type: ignore[method-assign]

    assert attempt_count == 3
    assert any("failed after 3 attempts" in s for s in stderr_messages)


async def test_notify_webhook_exception_retries(tmp_path: Path) -> None:
    """Retries on connection exceptions."""
    import sys
    stderr_messages: list[str] = []
    original_write = sys.stderr.write

    def capture(s: str) -> None:
        stderr_messages.append(s)

    sys.stderr.write = capture  # type: ignore[method-assign]

    attempt_count = 0

    try:
        async def mock_post(url: str, json: Any) -> None:
            nonlocal attempt_count
            attempt_count += 1
            raise ConnectionError("connection refused")

        mock_client = MagicMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                await _notify_webhook("https://example.com/hook", "run-1", "reason", {})
    finally:
        sys.stderr.write = original_write  # type: ignore[method-assign]

    assert attempt_count == 3
    assert any("failed after 3 attempts" in s for s in stderr_messages)


# ---------------------------------------------------------------------------
# await_decision — timeout escalation
# ---------------------------------------------------------------------------


async def test_await_decision_auto_approve(tmp_path: Path) -> None:
    """HORIZONX_HITL_AUTO_APPROVE=1 returns approve immediately."""
    run = _make_run(tmp_path)
    cfg = HITLConfig()

    with patch.dict("os.environ", {"HORIZONX_HITL_AUTO_APPROVE": "1"}):
        decision = await await_decision(run, "test", {}, cfg)

    assert decision.action == "approve"
    assert "auto-approved" in decision.instruction


async def test_await_decision_file_drop(tmp_path: Path) -> None:
    """Decision file written by operator is read and returned correctly."""
    run = _make_run(tmp_path)
    cfg = HITLConfig()
    decision_path = run.workspace_path / ".hitl_decision.json"

    # Write decision file before await_decision checks for it
    payload = HITLDecision(action="abort", instruction="too risky")
    decision_path.write_text(json.dumps(payload.model_dump(mode="json"), default=str))

    # Patch stdin to appear non-interactive (not a tty)
    with patch("sys.stdin") as mock_stdin:
        mock_stdin.isatty.return_value = False
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("HORIZONX_HITL_AUTO_APPROVE", None)
            result = await await_decision(run, "validator failed", {}, cfg)

    assert result.action == "abort"
    assert result.instruction == "too risky"
    assert not decision_path.exists()  # file deleted after read


async def test_await_decision_timeout_escalation(tmp_path: Path) -> None:
    """Timeout triggers escalation_action (abort) after timeout_minutes."""
    run = _make_run(tmp_path)
    cfg = HITLConfig(
        timeout_minutes=1,
        escalation_action="abort",
    )

    call_count = 0
    fake_start = time.monotonic()

    async def fast_sleep(delay: float) -> None:
        nonlocal call_count, fake_start
        call_count += 1
        # After first sleep, simulate that timeout has elapsed
        if call_count >= 1:
            # Monkey-patch monotonic so elapsed time appears to exceed timeout
            nonlocal fake_start
            fake_start = time.monotonic() - 120  # 2 minutes past

    # We patch time.monotonic to simulate elapsed time
    _original_monotonic = time.monotonic  # noqa: F841
    call_to_monotonic = [0]

    def patched_monotonic() -> float:
        call_to_monotonic[0] += 1
        if call_to_monotonic[0] == 1:
            return 0.0  # start time capture
        return 61.0  # subsequent calls — 61 seconds elapsed > 60s timeout

    with patch("sys.stdin") as mock_stdin:
        mock_stdin.isatty.return_value = False
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("HORIZONX_HITL_AUTO_APPROVE", None)
            with patch("asyncio.sleep", new_callable=AsyncMock):
                with patch("horizonx.hitl.gate.time") as mock_time:
                    mock_time.monotonic = patched_monotonic
                    result = await await_decision(run, "spin detected", {}, cfg)

    assert result.action == "abort"
    assert "timeout" in result.instruction.lower()


async def test_await_decision_timeout_defaults_to_approve(tmp_path: Path) -> None:
    """If escalation_action is None, timeout defaults to approve."""
    run = _make_run(tmp_path)
    cfg = HITLConfig(
        timeout_minutes=1,
        escalation_action=None,
    )

    call_to_monotonic = [0]

    def patched_monotonic() -> float:
        call_to_monotonic[0] += 1
        if call_to_monotonic[0] == 1:
            return 0.0
        return 65.0  # elapsed > 60s

    with patch("sys.stdin") as mock_stdin:
        mock_stdin.isatty.return_value = False
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("HORIZONX_HITL_AUTO_APPROVE", None)
            with patch("asyncio.sleep", new_callable=AsyncMock):
                with patch("horizonx.hitl.gate.time") as mock_time:
                    mock_time.monotonic = patched_monotonic
                    result = await await_decision(run, "spin", {}, cfg)

    assert result.action == "approve"
    assert "timeout" in result.instruction.lower()


async def test_console_timeout_sends_secondary_slack_escalation(tmp_path: Path) -> None:
    run = _make_run(tmp_path)
    cfg = HITLConfig(
        notification_type="console",
        timeout_minutes=1,
        escalation_channel="#on-call",
        escalation_action="abort",
    )
    with patch("sys.stdin") as stdin:
        stdin.isatty.return_value = False
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("HORIZONX_HITL_AUTO_APPROVE", None)
            with patch("asyncio.sleep", new_callable=AsyncMock):
                with patch("horizonx.hitl.gate.time") as mock_time:
                    mock_time.monotonic.side_effect = [0.0, 61.0]
                    with patch(
                        "horizonx.hitl.gate._notify_slack", new_callable=AsyncMock
                    ) as notify:
                        decision = await await_decision(run, "spin", {}, cfg)
    assert decision.action == "abort"
    notify.assert_awaited_once()
    assert notify.await_args.args[0] == "#on-call"


# ---------------------------------------------------------------------------
# HITLConfig fields
# ---------------------------------------------------------------------------


def test_hitlconfig_new_fields_default_none() -> None:
    """New HITLConfig fields are optional and default to None."""
    cfg = HITLConfig()
    assert cfg.timeout_minutes is None
    assert cfg.escalation_channel is None
    assert cfg.escalation_action is None


def test_hitlconfig_new_fields_set() -> None:
    """New HITLConfig fields can be set."""
    cfg = HITLConfig(
        timeout_minutes=30,
        escalation_channel="#escalation",
        escalation_action="abort",
    )
    assert cfg.timeout_minutes == 30
    assert cfg.escalation_channel == "#escalation"
    assert cfg.escalation_action == "abort"
