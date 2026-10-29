"""WIP exploration for #524 (not part of the PR)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from openminion import Agent
from tests.runtime.test_turn_capture_authorization import (
    AGENT_ID,
    MESSAGE,
    NEUTRAL_CONSTRAINT,
    WRITE_CONSTRAINT,
    _Provider,
    _record,
    _runtime,
)

ARGS = {
    "record_type": "fact",
    "key": "fact:editor_font",
    "title": "Editor font",
    "content": "The user writes code in Iosevka 14.",
}


class _ArgsProvider(_Provider):
    def respond(self, body: dict[str, Any]) -> dict[str, Any]:
        reply = super().respond(body)
        for call in reply["choices"][0]["message"].get("tool_calls") or []:
            if call["function"]["name"] == "memory_write":
                call["function"]["arguments"] = json.dumps(ARGS)
        return reply


@pytest.fixture
def provider() -> Any:
    server = _ArgsProvider()
    try:
        yield server
    finally:
        server.close()


def _rows(runtime: Any) -> dict[str, Any]:
    store = runtime.resolve_memory_assembly(AGENT_ID).service._store
    connection = sqlite3.connect(str(store._store.db_path))
    try:
        candidates = connection.execute(
            "SELECT type, title, content_json, meta_json, status"
            " FROM memory_candidates ORDER BY created_at, candidate_id"
        ).fetchall()
        records = connection.execute(
            "SELECT type, key, title FROM memory_records ORDER BY created_at, id"
        ).fetchall()
    finally:
        connection.close()
    return {
        "candidates": [list(row) for row in candidates],
        "records": [list(row) for row in records],
    }


def _event_types(runtime: Any, session_id: str) -> list[str]:
    sessions = runtime.sessions
    out = []
    for session in sessions.list_sessions(limit=100):
        for event in sessions.list_events(session_id=session.id, limit=1000):
            if session.id == session_id or event.session_id == session_id:
                out.append(f"{session.id}:{event.event_type}")
    return out


def _run(runtime: Any, text: str, session_id: str, forced: bool = True, **kw: Any):
    with Agent(
        runtime=runtime, forced_tools=["memory.write"] if forced else None
    ) as agent:
        return agent.run(text, session_id=session_id, **kw)


@pytest.mark.parametrize(
    "scenario",
    [
        "denied-neutral",
        "denied-noid",
        "pending-neutral",
        "pending-noid",
        "approved-ok",
        "approved-fail",
        "approved-fail-write-constraint",
        "later-yes-ok",
        "later-yes-fail",
        "later-yes-ok-noforced",
    ],
)
def test_explore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: _ArgsProvider,
    scenario: str,
) -> None:
    constraint = None if "noid" in scenario else NEUTRAL_CONSTRAINT
    if "write-constraint" in scenario:
        constraint = WRITE_CONSTRAINT
    runtime = _runtime(tmp_path, monkeypatch, provider, constraint=constraint)
    obs: dict[str, Any] = {"scenario": scenario, "turns": []}
    handler_calls: list[Any] = []
    try:
        recorder = _record(runtime, monkeypatch)
        if "fail" in scenario:
            spec = runtime.tools.get("memory.write")

            def failing(args: Any, ctx: Any) -> Any:
                handler_calls.append(dict(args))
                raise RuntimeError("memory backend unavailable")

            monkeypatch.setattr(spec, "handler", failing)
        approvals: list[Any] = []

        def cb(approve: bool):
            def callback(*args: Any) -> bool:
                approvals.append(args[:2])
                return approve

            return callback

        provider.write_tool_call = True
        steps: list[tuple[str, dict[str, Any], bool]] = []
        if scenario.startswith("denied"):
            steps = [(MESSAGE, {"approval_callback": cb(False)}, True)]
        elif scenario.startswith("pending"):
            steps = [(MESSAGE, {}, True)]
        elif scenario.startswith("approved"):
            steps = [(MESSAGE, {"approval_callback": cb(True)}, True)]
        else:
            steps = [(MESSAGE, {}, True), ("yes", {}, "noforced" not in scenario)]
        for text, kw, forced in steps:
            before = _rows(runtime)
            mark = len(provider.requests)
            dmark = len(recorder.decisions)
            try:
                result = _run(runtime, text, "s1", forced=forced, **kw)
                outcome = {"run_state": result.run_state, "text": result.text}
            except Exception as exc:  # noqa: BLE001
                outcome = {"error": repr(exc)}
            after = _rows(runtime)
            obs["turns"].append(
                {
                    "text": text,
                    **outcome,
                    "requests": provider.requests[mark:],
                    "decisions": recorder.decisions[dmark:],
                    "new_candidates": after["candidates"][len(before["candidates"]) :],
                    "new_records": after["records"][len(before["records"]) :],
                }
            )
        obs["approvals"] = approvals
        obs["handler_calls"] = handler_calls
        obs["events"] = _event_types(runtime, "s1")
    finally:
        runtime.close()
    print("\nOBS " + json.dumps(obs, default=str))
