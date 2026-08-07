"""Static contract tests for the durable timeline dashboard."""

import re
from pathlib import Path

STATIC_DIR = Path(__file__).parents[1] / "horizonx" / "dashboard" / "static"


def test_run_detail_exposes_interactive_timeline_playback_contract() -> None:
    """The run detail owns a single, on-demand durable timeline experience."""
    html = (STATIC_DIR / "index.html").read_text()
    script = (STATIC_DIR / "app.js").read_text()

    for marker in (
        'id="timeline-panel"',
        'id="timeline-events"',
        'id="timeline-detail"',
        'id="timeline-load-more"',
        'id="timeline-return-current"',
    ):
        assert marker in html

    assert "async function loadTimelinePage" in script
    assert "async function selectTimelineEvent" in script
    assert "timeline/playback?sequence=${sequence}" in script
    assert "timeline/${sequence}`" in script
    assert "window.returnToCurrentGraph" in script


def test_timeline_list_does_not_eagerly_fetch_event_payloads() -> None:
    """Payload detail is fetched only after an operator selects an event."""
    script = (STATIC_DIR / "app.js").read_text()

    selection_start = script.index("async function selectTimelineEvent")
    timeline_start = script.index("async function loadTimelinePage")
    timeline_end = script.index("window.loadMoreTimeline", timeline_start)

    assert "timeline/${sequence}`" not in script[timeline_start:timeline_end]
    assert "timeline/${sequence}`" in script[selection_start:]


def test_live_timeline_cursor_is_independent_from_manual_pagination() -> None:
    """SSE refreshes advance from the latest known event, never a page cursor."""
    script = (STATIC_DIR / "app.js").read_text()

    assert "timelineLiveAfter" in script
    assert "live ? state.timelineLiveAfter : state.timelineNextAfter" in script
    assert "state.timelineLiveAfter = Math.max" in script


def test_timeline_subscribes_to_recovery_and_fork_events() -> None:
    """Named SSE events refresh the durable timeline after recovery or a fork."""
    script = (STATIC_DIR / "app.js").read_text()

    for event_type in (
        "recovery.planned",
        "fork.created",
        "fork.merged",
    ):
        assert f"'{event_type}'" in script


def test_timeline_state_is_derived_from_durable_recovery_and_fork_events() -> None:
    """Recovery/fork are event facts, not unreachable RunStatus values."""
    script = (STATIC_DIR / "app.js").read_text()

    assert "function deriveTimelineRunState" in script
    assert "recovery.planned" in script
    assert "fork.created" in script
    assert "status === 'recovered'" not in script
    assert "status === 'forked'" not in script


def test_dashboard_named_sse_subscriptions_match_authoritative_event_types() -> None:
    """Every named SSE type must reach the durable timeline updater."""
    event_bus = (STATIC_DIR.parents[1] / "core" / "event_bus.py").read_text()
    script = (STATIC_DIR / "app.js").read_text()

    event_type_block = re.search(r"EventType = Literal\[(.*?)\]", event_bus, re.DOTALL)
    subscription_block = re.search(
        r"const DASHBOARD_EVENT_TYPES = \[(.*?)\];", script, re.DOTALL
    )
    assert event_type_block is not None
    assert subscription_block is not None

    authoritative = re.findall(r'"([^\"]+)"', event_type_block.group(1))
    subscribed = re.findall(r"'([^']+)'", subscription_block.group(1))
    assert subscribed == authoritative


def test_live_graph_reload_and_playback_exit_are_explicit() -> None:
    """A durable graph change refreshes current view, and playback can be exited."""
    script = (STATIC_DIR / "app.js").read_text()

    assert "event.type==='goals.graph_changed'" in script
    assert "window.returnToCurrentGraph = () =>" in script
    assert "state.timelineSelectedSequence = null" in script
    assert "loadGoals(state.currentRunId)" in script


def test_live_stream_does_not_render_raw_event_payloads() -> None:
    """Only selected-event detail may render a durable event payload."""
    script = (STATIC_DIR / "app.js").read_text()
    append_start = script.index("function appendEvent")
    append_end = script.index("// ─────────────────────────────────────────────────────────────────────────────\n// Center tab", append_start)

    assert "liveEventMetadata" in script[append_start:append_end]
    assert "JSON.stringify(event.payload)" not in script[append_start:append_end]
