from __future__ import annotations

from unittest.mock import patch

from openminion.modules.storage.runtime.migrations import migrate_database
from openminion.modules.storage.runtime.session_store import SessionStore
from openminion.modules.storage.runtime.sqlite import connect_database
from openminion.modules.context.slices import (
    SUMMARY_SHORT_SOURCE,
    build_session_slice_from_runtime_store,
)

EVENT_CLOCK = "openminion.modules.storage.runtime.session_store.lifecycle.utc_now_iso"


def _build_store(tmp_path) -> tuple[object, SessionStore]:
    db_path = tmp_path / "openminion.db"
    migrate_database(db_path)
    connection = connect_database(db_path)
    return connection, SessionStore(connection)


def test_maps_runtime_slice_fields(tmp_path) -> None:
    connection, store = _build_store(tmp_path)
    try:
        session = store.resolve_session(
            agent_id="main", channel="console", target="slice"
        )
        store.append_message(session_id=session.id, role="inbound", body="hello")
        store.append_message(session_id=session.id, role="outbound", body="world")
        ctx = store.ensure_session_context(session_id=session.id)
        store.update_session_context(
            session_id=session.id,
            summary_short="line one",
            rolling_summary="line one\nline two",
            version=ctx.version + 1,
        )
        store.append_event(
            session_id=session.id,
            event_type="session.compaction.archive",
            payload={"relative_path": "archive/chunk-1.jsonl"},
        )
        store.append_event(
            session_id=session.id,
            event_type="tool.call",
            payload={"tool_name": "web.search", "summary": "searched docs"},
        )

        slice_v15 = build_session_slice_from_runtime_store(
            store=store,
            session_id=session.id,
            limits={"recent_turn_limit": 5, "tool_events_limit": 3},
        )
    finally:
        connection.close()

    assert slice_v15.summary_short == "line one"
    assert slice_v15.summary_long == "line one\nline two"
    assert [turn.role for turn in slice_v15.recent_turns] == ["user", "assistant"]
    assert "archive/chunk-1.jsonl" in slice_v15.archive_refs
    assert slice_v15.recent_tool_events[0].tool_name == "web.search"


def test_missing_session_returns_empty_slice(tmp_path) -> None:
    connection, store = _build_store(tmp_path)
    try:
        slice_v15 = build_session_slice_from_runtime_store(
            store=store,
            session_id="missing",
            limits={"recent_turn_limit": 5, "tool_events_limit": 3},
        )
    finally:
        connection.close()

    assert slice_v15.summary_short == ""
    assert slice_v15.summary_long is None
    assert slice_v15.recent_turns == []
    assert slice_v15.archive_refs == []


def test_summary_short_does_not_fallback_to_recent_turn(tmp_path) -> None:
    connection, store = _build_store(tmp_path)
    try:
        session = store.resolve_session(
            agent_id="main", channel="console", target="fallback"
        )
        store.append_message(session_id=session.id, role="inbound", body="question")
        store.append_message(session_id=session.id, role="outbound", body="answer")

        slice_v15 = build_session_slice_from_runtime_store(
            store=store,
            session_id=session.id,
            limits={"recent_turn_limit": 5, "tool_events_limit": 0},
        )
    finally:
        connection.close()

    assert slice_v15.summary_long is None
    assert slice_v15.summary_short == ""
    assert SUMMARY_SHORT_SOURCE == "session_context.summary_short"


def test_last_event_follows_insertion_order_when_clock_steps_back(tmp_path) -> None:
    connection, store = _build_store(tmp_path)
    try:
        session = store.resolve_session(
            agent_id="main", channel="console", target="clock-step-back"
        )
        # The wall clock steps back ~1.3 s before the second tool event.
        stamps = [
            "2026-10-21T17:17:00.400000+00:00",
            "2026-10-21T17:16:59.100000+00:00",
        ]
        with patch(EVENT_CLOCK, side_effect=stamps):
            appended = [
                store.append_event(
                    session_id=session.id,
                    event_type="tool.call",
                    payload={"tool_name": tool_name, "summary": tool_name},
                )
                for tool_name in ("web.search", "web.fetch")
            ]

        slice_v15 = build_session_slice_from_runtime_store(
            store=store,
            session_id=session.id,
            limits={"recent_turn_limit": 5, "tool_events_limit": 3},
        )
        high_water = store.event_high_water(session_id=session.id)
    finally:
        connection.close()

    assert slice_v15.last_event_id == str(appended[-1].id) == str(high_water)
    assert [event.tool_name for event in slice_v15.recent_tool_events] == [
        "web.search",
        "web.fetch",
    ]
