"""A second `Agent.run` on the same session does not answer a waiting confirmation.

Installed package only (no repo test fixtures); run with `python -I -X utf8`
from an empty directory outside any checkout. It reuses the #448 probe's
helpers (`probe_420_lib.py`, `_common.py`, unchanged): a real `APIRuntime`
for one agent, `openminion`, whose `openai` provider is a scripted
OpenAI-compatible server on 127.0.0.1 (no external network, dummy key), and
call-through recorders that never change a result.

Each path has its own fresh runtime home and one
`Agent(runtime=runtime, forced_tools=["memory.write"])`, with no
`approval_callback`. In call 1 the scripted model calls `memory_write` once
with fixed arguments; with no callback that call waits for the user's
confirmation. Call 2 answers "yes":

  (a) normal two-call path: `agent.run("yes")`, a second call on the same
      Agent object, so the same session id. `Agent.run` takes no thread.
      Call 3 (an observation, after the two calls): "yes" on call 1's thread,
      as in (b), to see whether the confirmation is still waiting there.
  (b) explicit-thread control: `runtime.run_turn(payload=...)`, where the
      payload is the one `Agent.run` builds for "yes" (`Agent._build_payload`)
      plus call 1's `thread_id` in `inbound_metadata`.

Recorded per call: session id, thread id, thread decision action and reason,
run id and run state, respond kind, pending confirmation command id, reply
text, the scripted model requests, the `memory.write` handler calls, the
`fact:editor_font` record, and the session's run, response and confirmation
events with their thread and run ids.

Usage: python -I -X utf8 probe_agent_run_confirmation_resume.py <work_dir> <out.json> [--expect-version V]
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from importlib import metadata
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, write_result  # noqa: E402

WORK = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
EXPECT = sys.argv[sys.argv.index("--expect-version") + 1] if "--expect-version" in sys.argv else None
WORK.mkdir(parents=True, exist_ok=True)
quiet()
clear_override_env()


def _dist(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _direct_url() -> Any:
    try:
        text = metadata.distribution("openminion").read_text("direct_url.json")
        return json.loads(text) if text else None
    except Exception:  # noqa: BLE001
        return None


info = banner("probe_agent_run_confirmation_resume",
              provider="openai (OpenAI-compatible HTTP) -> scripted ThreadingHTTPServer on 127.0.0.1",
              dist_versions={n: _dist(n) for n in ("openminion", "prompt_toolkit", "pydantic")},
              openminion_direct_url=_direct_url())

from openminion import Agent  # noqa: E402
from openminion.modules.controlplane.constants import CALLER_HANDLES_DELIVERY_METADATA_KEY  # noqa: E402

import probe_420_lib as lib  # noqa: E402
from probe_420_lib import (  # noqa: E402
    AGENT_ID,
    CAPTURE_MESSAGE,
    ScriptedProvider,
    build_runtime,
    chdir_env,
    restore_env,
    session_events,
    sqlite_rows,
)

TOOL_ARGS = {"record_type": "fact", "key": "fact:editor_font", "title": "Editor font",
             "content": "The user writes code in Iosevka 14."}
META_KEYS = ("thread_id", "conversation_id", "thread_decision_action", "thread_decision_reason",
             "respond_kind", "pending_confirmation_command_id", "brain_status")
EVENT_KEYS = ("run_id", "thread_id", "state", "thread_decision_action", "thread_decision_reason",
              "respond_kind", "command_id", "tool_name", "approval_id", "delivery_mode")
EVENT_PREFIXES = ("run.", "response.", "thread.", "turn.", "policy_", "approval", "tool.")
c = Checks()


class ToolCallProvider(ScriptedProvider):
    """The #448 probe's scripted server, plus one `memory_write` call when armed."""

    def __init__(self) -> None:
        self.write_tool_call = False
        super().__init__()

    def respond(self, body: dict[str, Any], path: str) -> dict[str, Any]:
        names = [str((t.get("function") or {}).get("name", "")) for t in body.get("tools") or []
                 if isinstance(t, dict)]
        choice = body.get("tool_choice")
        forced = str((choice.get("function") or {}).get("name", "")) if isinstance(choice, dict) else ""
        with self.lock:
            call = forced != "submit_output" and self.write_tool_call and "memory_write" in names
            if call:
                self.write_tool_call = False
                index = len(self.requests)
                self.requests.append({"index": index, "kind": "tool_call:memory_write", "path": path,
                                      "model": body.get("model"), "t": time.monotonic()})
        if call:
            tool_call = {"id": f"call-memory-write-{index}", "type": "function",
                         "function": {"name": "memory_write", "arguments": json.dumps(TOOL_ARGS)}}
            return lib._completion({"role": "assistant", "content": None, "tool_calls": [tool_call]},
                                   "tool_calls")
        return super().respond(body, path)


def fact_records(runtime: Any) -> Any:
    try:
        store = runtime.resolve_memory_assembly(AGENT_ID).service._store
        db = Path(str(getattr(store, "_store", store).db_path))
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"
    return sqlite_rows(db, "SELECT type, key, title FROM memory_records WHERE key = ? AND is_deleted = 0",
                       (TOOL_ARGS["key"],))


def events(runtime: Any, session_id: str) -> list[dict[str, Any]]:
    out = []
    for event in session_events(runtime.sessions, session_id=session_id):
        if not event.event_type.startswith(EVENT_PREFIXES):
            continue
        payload = event.payload if isinstance(event.payload, dict) else {}
        out.append({"id": event.id, "event_type": event.event_type,
                    **{k: payload.get(k) for k in EVENT_KEYS if payload.get(k) not in (None, "")}})
    return out


def call_view(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):  # APIRuntime.run_turn
        meta = dict(result.get("metadata") or {})
        return {"via": "APIRuntime.run_turn", "session_id": result.get("session_id"), "run_id": result.get("run_id"),
                "run_state": result.get("run_state"), "text": str(result.get("body") or "")[:200],
                **{k: meta.get(k) for k in META_KEYS}}
    meta = dict(result.metadata or {})
    return {"via": "Agent.run", "session_id": result.session_id, "run_id": result.run_id,
            "run_state": result.run_state, "text": str(result.text or "")[:200],
            **{k: meta.get(k) for k in META_KEYS}}


def call(ctx: dict[str, Any], label: str, fn: Any, *, arm: bool) -> dict[str, Any]:
    provider, runtime, handled = ctx["provider"], ctx["runtime"], ctx["handled"]
    mark_r, mark_h = len(provider.requests), len(handled)
    provider.write_tool_call = arm
    try:
        row = call_view(fn())
    except Exception as exc:  # noqa: BLE001 - recorded; the checks then fail
        row = {"error": f"{type(exc).__name__}: {exc}",
               "frames": [f"{Path(f.filename).name}:{f.lineno} {f.name}"
                          for f in traceback.extract_tb(exc.__traceback__)][-6:]}
    provider.write_tool_call = False
    row["label"] = label
    row["model_requests"] = [r["kind"] for r in provider.requests[mark_r:]]
    row["memory_write_handler_calls"] = handled[mark_h:]
    row["fact_records_after"] = fact_records(runtime)
    ctx["calls"].append(row)
    return row


def explicit_thread_reply(agent: Any, runtime: Any, thread_id: str) -> Any:
    payload = agent._build_payload("yes")
    payload["inbound_metadata"] = {**payload.get("inbound_metadata", {}), "thread_id": thread_id}
    return runtime.run_turn(payload=payload)


def run_path(label: str) -> dict[str, Any]:
    root = WORK / label
    provider = ToolCallProvider()
    old_env = chdir_env(root)
    row: dict[str, Any] = {"path": label}
    runtime = None
    try:
        runtime = build_runtime(root, provider)
        spec = runtime.tools.get("memory.write")
        handled: list[dict[str, Any]] = []
        original = spec.handler

        def recording_handler(args: Any, context: Any) -> Any:
            handled.append({k: (args or {}).get(k) for k in TOOL_ARGS})
            return original(args, context)

        spec.handler = recording_handler
        ctx = {"provider": provider, "runtime": runtime, "handled": handled, "calls": []}
        with Agent(runtime=runtime, forced_tools=["memory.write"]) as agent:
            row["agent_session_id"] = agent.session_id
            first = call(ctx, "call 1: agent.run(capture message)", lambda: agent.run(CAPTURE_MESSAGE), arm=True)
            thread_1 = str(first.get("thread_id") or "")
            if label == "a":
                call(ctx, "call 2: agent.run('yes')", lambda: agent.run("yes"), arm=False)
                call(ctx, "call 3 (observation): 'yes' on call 1's thread",
                     lambda: explicit_thread_reply(agent, runtime, thread_1), arm=False)
            else:
                call(ctx, "call 2: 'yes' on call 1's thread (APIRuntime.run_turn, Agent payload + thread_id)",
                     lambda: explicit_thread_reply(agent, runtime, thread_1), arm=False)
        spec.handler = original
        row["calls"] = ctx["calls"]
        row["session_events"] = events(runtime, row["agent_session_id"])
        row["provider_hosts"] = sorted({provider.base_url.split("/")[2].split(":")[0]})
    except Exception as exc:  # noqa: BLE001 - recorded; the checks then fail
        row["path_error"] = f"{type(exc).__name__}: {exc}"
        row["path_error_frames"] = [f"{Path(f.filename).name}:{f.lineno} {f.name}"
                                    for f in traceback.extract_tb(exc.__traceback__)][-8:]
    finally:
        if runtime is not None:
            try:
                runtime.close()
            except Exception as exc:  # noqa: BLE001
                row["runtime_close"] = f"{type(exc).__name__}: {exc}"
        restore_env(old_env)
        provider.close()
    return row


paths = {"a": run_path("a"), "b": run_path("b")}


def calls(label: str) -> list[dict[str, Any]]:
    return paths[label].get("calls") or [{}, {}, {}]


def waiting(first: dict[str, Any]) -> bool:
    return (first.get("respond_kind") == "policy_confirmation_prompt"
            and bool(first.get("pending_confirmation_command_id"))
            and "tool_call:memory_write" in first.get("model_requests", [])
            and not first.get("memory_write_handler_calls") and first.get("fact_records_after") == [])


a1, a2, *a_rest = calls("a") + [{}]
a3 = a_rest[0] if a_rest else {}
b1, b2, *_ = calls("b") + [{}]
if EXPECT is not None:
    c.check(f"installed openminion is {EXPECT}", info.get("openminion.__version__") == EXPECT,
            info.get("openminion.__version__"))
for label, first in (("a", a1), ("b", b1)):
    c.check(f"({label}) call 1: the model's memory.write call waits for the user's confirmation "
            "(respond_kind policy_confirmation_prompt, a pending_confirmation_command_id); the handler did not "
            "run and there is no fact:editor_font record", waiting(first), first)
c.check("(a) call 2, a second agent.run('yes') on the same Agent: same session id as call 1, a new thread "
        "(thread_decision_action fork_thread, reason settled_without_resume), no pending confirmation on it",
        bool(a2.get("session_id")) and a2.get("session_id") == a1.get("session_id")
        and bool(a2.get("thread_id")) and a2.get("thread_id") != a1.get("thread_id")
        and a2.get("thread_decision_action") == "fork_thread"
        and a2.get("thread_decision_reason") == "settled_without_resume"
        and not a2.get("pending_confirmation_command_id"),
        {k: a2.get(k) for k in ("session_id", "thread_id", "thread_decision_action", "thread_decision_reason",
                                "pending_confirmation_command_id", "respond_kind")})
c.check("(a) call 2: the waiting memory.write call does not run (no handler call, no fact:editor_font record); "
        "the reply comes from a new model request",
        not a2.get("memory_write_handler_calls") and a2.get("fact_records_after") == []
        and bool(a2.get("model_requests")),
        {k: a2.get(k) for k in ("text", "respond_kind", "model_requests", "memory_write_handler_calls",
                                "fact_records_after")})
c.check("(b) control, 'yes' with call 1's thread_id: same session and thread (resume_thread, "
        "explicit_thread_requested); the waiting call runs once with the model's arguments and writes the record",
        b2.get("session_id") == b1.get("session_id") and bool(b2.get("thread_id"))
        and b2.get("thread_id") == b1.get("thread_id")
        and b2.get("thread_decision_action") == "resume_thread"
        and b2.get("thread_decision_reason") == "explicit_thread_requested"
        and b2.get("memory_write_handler_calls") == [TOOL_ARGS]
        and isinstance(b2.get("fact_records_after"), list) and len(b2["fact_records_after"]) == 1,
        {k: b2.get(k) for k in ("session_id", "thread_id", "thread_decision_action", "thread_decision_reason",
                                "memory_write_handler_calls", "fact_records_after", "text")})
hosts = sorted({h for p in paths.values() for h in p.get("provider_hosts", [])})
errors = {k: (p.get("path_error"), p.get("path_error_frames")) for k, p in paths.items() if p.get("path_error")}
call_errors = [x.get("error") for p in paths.values() for x in p.get("calls", []) if x.get("error")]
c.check("Offline provider and no error: both paths' openai base URL is the scripted server on 127.0.0.1",
        hosts == ["127.0.0.1"] and not errors and not call_errors,
        {"hosts": hosts, "errors": errors, "call_errors": call_errors})

observations = {
    "a_call_3_explicit_thread_after_fork": {k: a3.get(k) for k in (
        "session_id", "thread_id", "thread_decision_action", "thread_decision_reason", "respond_kind",
        "memory_write_handler_calls", "fact_records_after", "text", "error")},
}
write_result(OUT, {"info": info, "summary": c.summary(), "all_pass": c.all_pass, "checks": c.items,
                   "observations": observations, "paths": paths})
for label in ("a", "b"):
    for row in paths[label].get("calls", []):
        print(f"[{label}] {row.get('label')}: session={row.get('session_id')} thread={row.get('thread_id')} "
              f"decision={row.get('thread_decision_action')}/{row.get('thread_decision_reason')} "
              f"run={row.get('run_id')} state={row.get('run_state')} respond_kind={row.get('respond_kind')} "
              f"pending={row.get('pending_confirmation_command_id')} handler_calls={len(row.get('memory_write_handler_calls') or [])} "
              f"records={row.get('fact_records_after')} requests={row.get('model_requests')} text={row.get('text')!r}")
summary = c.summary()
print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
sys.exit(0 if c.all_pass else 1)
