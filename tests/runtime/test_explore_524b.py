"""WIP exploration 4 for #524: later approval of a pending call (not part of the PR)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from openminion import Agent
from openminion.modules.controlplane.constants import (
    CALLER_HANDLES_DELIVERY_METADATA_KEY,
)
from tests.runtime.test_explore_524 import (  # noqa: F401
    ARGS,
    _ArgsProvider,
    _rows,
    provider,
)
from tests.runtime.test_turn_capture_authorization import (
    AGENT_ID,
    MESSAGE,
    NEUTRAL_CONSTRAINT,
    _record,
    _runtime,
)

META = (
    "thread_id",
    "thread_decision_action",
    "thread_decision_reason",
    "pending_confirmation_command_id",
    "brain_status",
    "respond_kind",
)


@pytest.mark.parametrize(
    "variant",
    ["yes-forced", "yes-plain", "yes-fail", "no", "yes-agent-resume", "allow_session"],
)
def test_explore_later(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: _ArgsProvider,
    variant: str,
) -> None:
    runtime = _runtime(tmp_path, monkeypatch, provider, constraint=NEUTRAL_CONSTRAINT)
    obs: dict[str, Any] = {"variant": variant, "turns": []}
    handled: list[Any] = []
    try:
        recorder = _record(runtime, monkeypatch)
        if variant == "yes-fail":

            def failing(args: Any, ctx: Any) -> Any:
                handled.append(dict(args))
                raise RuntimeError("memory backend unavailable")

            monkeypatch.setattr(runtime.tools.get("memory.write"), "handler", failing)
        provider.write_tool_call = True

        def record(text: str, fn: Any) -> dict[str, Any]:
            before = _rows(runtime)
            mark = len(provider.requests)
            dmark = len(recorder.decisions)
            try:
                body, meta, state = fn()
                outcome = {"run_state": state, "text": str(body)[:140], "meta": meta}
            except Exception as exc:  # noqa: BLE001
                outcome = {"error": repr(exc)}
                meta = {}
            after = _rows(runtime)
            obs["turns"].append(
                {
                    "input": text,
                    **outcome,
                    "requests": provider.requests[mark:],
                    "decisions": recorder.decisions[dmark:],
                    "new_candidates": [
                        c[:3] for c in after["candidates"][len(before["candidates"]) :]
                    ],
                    "new_records": after["records"][len(before["records"]) :],
                }
            )
            return meta

        def first() -> Any:
            with Agent(runtime=runtime, forced_tools=["memory.write"]) as agent:
                r = agent.run(MESSAGE, session_id="s1")
            return r.text, {k: r.metadata.get(k) for k in META}, r.run_state

        meta = record(MESSAGE, first)
        thread_id = meta.get("thread_id")
        reply = {"no": "no", "allow_session": "allow_session"}.get(variant, "yes")

        def second() -> Any:
            if variant == "yes-agent-resume":
                with Agent(runtime=runtime, forced_tools=["memory.write"]) as agent:
                    r = agent.run(reply, session_id="s1")
                return r.text, {k: r.metadata.get(k) for k in META}, r.run_state
            payload: dict[str, Any] = {
                "message": reply,
                "session_id": "s1",
                "deliver": False,
                "inbound_metadata": {
                    CALLER_HANDLES_DELIVERY_METADATA_KEY: "true",
                    "thread_id": thread_id,
                },
            }
            if variant != "yes-plain":
                payload["forced_tools"] = ["memory.write"]
            raw = runtime.run_turn(payload=payload)
            m = dict(raw.get("metadata") or {})
            return raw.get("body"), {k: m.get(k) for k in META}, raw.get("run_state")

        record(reply, second)
        obs["handled"] = handled
    finally:
        runtime.close()
    print("\nOBS4 " + json.dumps(obs, default=str))
