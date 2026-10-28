"""Helpers for probe_420.py (installed package only, no repo test fixtures).

- ScriptedProvider: an OpenAI-compatible chat-completions server on 127.0.0.1
  (ThreadingHTTPServer, no external network). It answers the auto fact
  extraction (`purpose: reflect`, forced `submit_output`) with one
  user_preference item when the user message is the probe's capture message,
  and with no items otherwise; other forced structured calls get a neutral
  answer; text calls get a short reply. Every request is recorded in order.
- build_runtime: an `APIRuntime.from_manager` runtime for one agent
  (`openminion`, provider `openai` pointed at the scripted server), single
  process, explicit roots; optionally the bundled `reference-sqlite` external
  memory backend, a registered memory config (the PostgreSQL memory store of
  case P), or an identity profile with a hard constraint.
- Observers: call-through wrappers on the runtime's own objects (the memory
  assembly's recover_pending_captures, the agent service's capture candidate
  extractor, the memctl bundle write, the agent's security policy evaluate, the
  runtime manager's submit_turn and the lifecycle bridge's cron event hook). They record and call the original; they
  never replace a result. `fail_next_capture_writes` makes the next bundle
  write raise StoreWriteError (a transient store failure), as upstream's #420
  regression test does, so a turn ends with a pending capture intent.
- Cron helpers: the job is created by the installed `task.consolidate_memory`
  tool handler through the daemon's TaskManager; `run_task_now` queues a run
  (the `task.run_now` path); the daemon's own `attach_cron_scheduler` starts the
  real CronScheduler with the daemon's CronTurnExecutor on this runtime.
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

AGENT_ID = "openminion"
CAPTURE_MESSAGE = "Please remember that I prefer tabs over spaces for indentation."
CAPTURE_ITEM = {
    "kind": "user_preference",
    "normalized_key": "user_preference:indentation",
    "title": "Prefers tabs for indentation",
    "content": "The user prefers tabs over spaces for indentation.",
    "tags": ["probe-420"],
}
TERMINAL_RUN_STATES = ("finished", "failed", "timed_out", "cancelled")


# ---------------------------------------------------------------- scripted provider
def _completion(message: dict[str, Any], finish: str) -> dict[str, Any]:
    return {"id": "chatcmpl-probe-420", "object": "chat.completion", "created": 0, "model": "fixture-model",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}


def _submit(arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
    call = {"id": call_id, "type": "function", "function": {"name": "submit_output", "arguments": json.dumps(arguments)}}
    return _completion({"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls")


def _text(content: str) -> dict[str, Any]:
    return _completion({"role": "assistant", "content": content}, "stop")


def _all_text(body: dict[str, Any]) -> str:
    parts = []
    for message in body.get("messages") or []:
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        parts.append(str(content or ""))
    return "\n".join(parts)


class ScriptedProvider:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                payload = json.dumps(provider.respond(body, self.path)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def respond(self, body: dict[str, Any], path: str) -> dict[str, Any]:
        txt = _all_text(body)
        choice = body.get("tool_choice")
        forced = str((choice.get("function") or {}).get("name", "")) if isinstance(choice, dict) else ""
        if forced == "submit_output":
            if "purpose: reflect" in txt:
                kind = "extraction"
            elif "purpose: judge" in txt or "[JUDGE CONTEXT]" in txt:
                kind = "judge"
            elif "freshness-routing classifier" in txt:
                kind = "freshness"
            else:
                kind = "other_structured"
        elif "purpose: decide" in txt:
            kind = "decide"
        else:
            kind = "other_text"
        has_capture_text = CAPTURE_MESSAGE in txt
        with self.lock:
            index = len(self.requests)
            self.requests.append({"index": index, "kind": kind, "path": path, "model": body.get("model"),
                                  "mentions_capture_message": has_capture_text, "t": time.monotonic()})
        if kind == "extraction":
            items = [dict(CAPTURE_ITEM)] if has_capture_text else []
            return _submit({"items": items}, f"call-extract-{index}")
        if kind == "judge":
            return _submit({"final_answer": "Noted.", "satisfied": True, "next_action": "close"}, f"call-judge-{index}")
        if kind == "freshness":
            return _submit({"intent": "statement", "domain": "general", "time_sensitive": False,
                            "needs_live_data": False, "needs_sources": False, "needs_exact_date": False,
                            "answer_mode": "direct", "reason": "No live data needed.", "confidence": 1},
                           f"call-freshness-{index}")
        if kind == "other_structured":
            return _submit({}, f"call-other-{index}")
        return _text("Noted: tabs over spaces." if has_capture_text else "Consolidation reviewed; nothing to change.")

    def kinds_between(self, start: float, end: float) -> list[str]:
        with self.lock:
            return [r["kind"] for r in self.requests if start <= r["t"] <= end]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


# ---------------------------------------------------------------- runtime
PROFILE_YAML = """agent_id: openminion
display_name: Probe 420 agent
profile_revision: 1
role:
  mission: Answer the user briefly.
  responsibilities:
    - Answer the user briefly.
  hard_constraints:
    - {constraint}
personality:
  tone: direct
  verbosity: terse
risk:
  risk_level: low
tool_posture:
  tool_use: allowed
"""


def build_runtime(root: Path, provider: ScriptedProvider, *, external_adapter: str | None = None,
                  identity_constraint: str | None = None, memory_config: Any = None) -> Any:
    from openminion import APIRuntime
    from openminion.base.config import AgentProfileConfig, ConfigManager, OpenMinionConfig, save_config

    root.mkdir(parents=True, exist_ok=True)
    config = OpenMinionConfig()
    config.agents = {AGENT_ID: AgentProfileConfig(name=AGENT_ID, provider="openai", default_channel="console")}
    config.default_agent = AGENT_ID
    config.providers.openai.api_key_env = "PROBE_420_PROVIDER_KEY"
    config.providers.openai.base_url = provider.base_url
    config.runtime.process_mode = "single-process"
    config.runtime.daemon_auto_start = False
    config.storage.path = str(root / ".openminion" / "state.db")
    if identity_constraint is not None:
        identity_root = root / "identity"
        (identity_root / AGENT_ID).mkdir(parents=True, exist_ok=True)
        (identity_root / AGENT_ID / "profile.yaml").write_text(
            PROFILE_YAML.format(constraint=identity_constraint), encoding="utf-8")
        config.identity.bundle_root = str(identity_root)
        config.identity.db_path = str(root / ".openminion" / "identity.db")
    save_config(config, str(root / "agents.json"))
    manager = ConfigManager.load(str(root / "agents.json"), home_root=root, data_root=root / ".openminion")
    if external_adapter is not None:
        external_memory = {
            "store": {"backend": "sqlite", "sqlite_path": str(root / "memory.db")},
            "backend": {"provider": "external", "external_adapter": external_adapter,
                        "options": {"db_path": str(root / "external.sqlite3")}},
        }
        manager.register("memory", lambda **_kwargs: external_memory)
    elif memory_config is not None:
        manager.register("memory", memory_config)
    return APIRuntime.from_manager(manager)


# ---------------------------------------------------------------- observers
class Observers:
    """Call-through recorders on the runtime's own objects (see the module docstring)."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.timeline: list[dict[str, Any]] = []
        self.recoveries: list[dict[str, Any]] = []
        self.extractor_calls: list[dict[str, Any]] = []
        self.bundle_writes: list[dict[str, Any]] = []
        self.submitted_turns: list[dict[str, Any]] = []
        self.cron_events: list[dict[str, Any]] = []
        self.policy_decisions: list[dict[str, Any]] = []
        self.fail_next_capture_writes = 0
        self._undo: list[tuple[Any, str]] = []
        from openminion.services.agent.execution.composition import AgentServiceTurnFlowAdapter

        self.service = runtime.resolve_agent_service(AGENT_ID)
        self.assembly = runtime.resolve_memory_assembly(AGENT_ID)
        self.memctl = self.assembly.memctl
        self.brain_session_api = self.service._get_runner().session_api
        # The same object the cron executor passes to memory_capture_recovery_allowed.
        self.security_policy = AgentServiceTurnFlowAdapter(self.service).security_policy

    def _mark(self, what: str, **extra: Any) -> None:
        self.timeline.append({"seq": len(self.timeline), "what": what, "t": time.monotonic(),
                              "thread": threading.current_thread().name, **extra})

    def _wrap(self, obj: Any, name: str, make: Any) -> None:
        original = getattr(obj, name)
        setattr(obj, name, make(original))
        self._undo.append((obj, name))

    def install(self) -> "Observers":
        from openminion.modules.memory.errors import StoreWriteError

        obs = self
        runtime_sessions = self.runtime.sessions

        def recover(original: Any) -> Any:
            def wrapper(**kwargs: Any) -> Any:
                extractor = kwargs.get("extract_candidates")
                row: dict[str, Any] = {
                    "sessions_is_runtime_session_store": kwargs.get("sessions") is runtime_sessions,
                    "extract_candidates": getattr(extractor, "__qualname__", type(extractor).__name__),
                    "extract_candidates_is_service_method": extractor == obs.service.extract_memory_capture_candidates,
                    "limit": kwargs.get("limit"),
                }
                obs._mark("recovery.start")
                row["t_start"] = time.monotonic()
                try:
                    result = original(**kwargs)
                except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
                    row["raised"] = f"{type(exc).__name__}: {exc}"
                    row["t_end"] = time.monotonic()
                    obs.recoveries.append(row)
                    obs._mark("recovery.raised", error=row["raised"])
                    raise
                row["result"] = [result.scanned, result.recovered, result.pending]
                row["t_end"] = time.monotonic()
                obs.recoveries.append(row)
                obs._mark("recovery.end", result=row["result"])
                return result
            return wrapper

        def extract(original: Any) -> Any:
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                session_api = kwargs.get("session_api")
                row: dict[str, Any] = {
                    "positional_args": len(args),
                    "keywords": sorted(kwargs),
                    "session_api_type": type(session_api).__name__,
                    "session_api_is_brain_runner_session_api": session_api is obs.brain_session_api,
                    "session_api_is_runtime_session_store": session_api is runtime_sessions,
                    "session_id": kwargs.get("session_id"),
                    "root_turn_id": kwargs.get("root_turn_id"),
                    "user_message": kwargs.get("user_message"),
                }
                obs._mark("extractor.call", session_id=row["session_id"], root_turn_id=row["root_turn_id"])
                try:
                    items = original(*args, **kwargs)
                    row["items"] = [dict(i) for i in items]
                    return items
                except Exception as exc:  # noqa: BLE001
                    row["raised"] = f"{type(exc).__name__}: {exc}"
                    raise
                finally:
                    obs.extractor_calls.append(row)
            return wrapper

        def bundle_write(original: Any) -> Any:
            def wrapper(**kwargs: Any) -> Any:
                row: dict[str, Any] = {
                    "capture_id": kwargs.get("capture_id"), "root_turn_id": kwargs.get("root_turn_id"),
                    "session_id": kwargs.get("session_id"), "agent_id": kwargs.get("agent_id"),
                    "candidates": [dict(c) for c in (kwargs.get("candidates") or [])],
                    "thread": threading.current_thread().name,
                }
                obs._mark("bundle_write", capture_id=row["capture_id"])
                try:
                    if obs.fail_next_capture_writes > 0:
                        obs.fail_next_capture_writes -= 1
                        row["injected"] = "StoreWriteError"
                        raise StoreWriteError("probe: transient memory store write failure")
                    result = original(**kwargs)
                    row["result"] = dict(result) if isinstance(result, dict) else repr(result)
                    return result
                except Exception as exc:  # noqa: BLE001
                    row["raised"] = f"{type(exc).__name__}: {exc}"
                    raise
                finally:
                    obs.bundle_writes.append(row)
            return wrapper

        def submit(original: Any) -> Any:
            def wrapper(request: Any, *args: Any, **kwargs: Any) -> Any:
                row = {"session_id": getattr(request, "session_id", None), "agent_id": getattr(request, "agent_id", None),
                       "input_text": getattr(request, "input_text", None),
                       "thread": threading.current_thread().name}
                obs._mark("submit_turn", session_id=row["session_id"])
                obs.submitted_turns.append(row)
                return original(request, *args, **kwargs)
            return wrapper

        def evaluate(original: Any) -> Any:
            def wrapper(check: Any, *args: Any, **kwargs: Any) -> Any:
                decision = original(check, *args, **kwargs)
                context = getattr(check, "context", None)
                if getattr(context, "target", "") == "memory-capture-recovery":
                    obs.policy_decisions.append({
                        "tool_name": getattr(check.action, "tool_name", None), "channel": context.channel,
                        "target": context.target, "session_id": context.session_id, "run_id": context.run_id,
                        "decision": getattr(decision, "decision", None),
                        "reason_code": getattr(decision, "reason_code", None),
                        "allowed": bool(getattr(decision, "allowed", False)),
                        "details": dict(getattr(decision, "details", {}) or {})})
                    obs._mark("policy.decision", allowed=bool(getattr(decision, "allowed", False)))
                return decision
            return wrapper

        if self.security_policy is not None:
            self._wrap(self.security_policy, "evaluate", evaluate)
        self._wrap(self.assembly, "recover_pending_captures", recover)
        self._wrap(self.service, "extract_memory_capture_candidates", extract)
        self._wrap(self.memctl, "apply_capture_bundle", bundle_write)
        self._wrap(self.runtime.runtime_manager, "submit_turn", submit)
        bridge = getattr(self.runtime, "_lifecycle_event_bridge", None)
        if bridge is not None:
            def cron_hook(original: Any) -> Any:
                def wrapper(event_type: str, payload: dict[str, Any]) -> Any:
                    if event_type != "cron.scheduler.heartbeat":
                        keep = {k: payload.get(k) for k in ("run_id", "job_id", "state", "summary", "error")
                                if payload.get(k) is not None}
                        obs.cron_events.append({"event": event_type, **keep})
                        obs._mark(event_type, run_id=payload.get("run_id"))
                    return original(event_type, payload)
                return wrapper
            self._wrap(bridge, "handle_cron_event", cron_hook)
        return self

    def uninstall(self) -> None:
        for obj, name in reversed(self._undo):
            try:
                delattr(obj, name)
            except AttributeError:
                pass
        self._undo.clear()


# ---------------------------------------------------------------- cron
class Cron:
    """The daemon's cron store, TaskManager and scheduler for one runtime."""

    def __init__(self, runtime: Any, root: Path) -> None:
        from openminion.modules.task import TaskManager
        from openminion.modules.task.constants import DEFAULT_INTEGRATED_SQLITE_SUBPATH
        from openminion.services.runtime import daemon

        self.runtime = runtime
        self.root = root
        self.daemon = daemon
        resolve_db, resolve_brain_db, store_cls, _scheduler_cls = daemon._cron_scheduler_components()
        self.store = daemon._cron_store_for_runtime(runtime=runtime, resolve_database_path=resolve_db,
                                                    resolve_brain_sessions_db_path=resolve_brain_db,
                                                    sqlite_session_store=store_cls)
        self.task_manager = TaskManager.from_cron_repository(
            self.store, db_path=(Path(runtime.data_root) / DEFAULT_INTEGRATED_SQLITE_SUBPATH).resolve())
        self.scheduler: Any = None

    def create_consolidation_job(self, batch_limit: int = 8) -> dict[str, Any]:
        from openminion.modules.tool.runtime import RuntimeContext
        from openminion.modules.tool.runtime.policy import Policy
        from openminion.tools.task.plugin import _h_task_consolidate_memory

        workspace = self.root / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        policy = Policy(raw={"workspace_root": str(workspace), "context_metadata": {"agent_id": AGENT_ID},
                             "paths": {"read_allow": [str(workspace)], "write_allow": [str(workspace)], "deny": []},
                             "tools": {"allow_prefix": [""]}})
        ctx = RuntimeContext(policy=policy, workspace=workspace, run_root=self.root / "tool-run", scope="WRITE_SAFE",
                             confirm=False, task_manager=self.task_manager)
        return _h_task_consolidate_memory({"interval_hours": 12, "batch_limit": batch_limit}, ctx)

    def start(self) -> Any:
        self.scheduler = self.daemon.attach_cron_scheduler(runtime=self.runtime, daemon_id="probe-420-daemon",
                                                           daemon_component_id="primary", tick_seconds=0.2,
                                                           lease_ttl_seconds=60, max_concurrent_runs=5)
        return self.scheduler

    def run_now(self, task_id: str) -> dict[str, Any]:
        return dict(self.task_manager.run_task_now(task_id))

    def wait(self, job_id: str, run_id: str, timeout_s: float = 180.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        row: dict[str, Any] = {}
        while time.monotonic() < deadline:
            rows = [r for r in self.store.list_cron_runs(job_id=job_id, limit=50) if r.get("run_id") == run_id]
            row = dict(rows[0]) if rows else {}
            if row.get("state") in TERMINAL_RUN_STATES:
                return row
            time.sleep(0.2)
        row["probe_wait"] = f"not terminal after {timeout_s} s"
        return row

    def stop(self) -> None:
        if self.scheduler is not None:
            self.scheduler.shutdown(grace_s=5)
            self.scheduler = None


# ---------------------------------------------------------------- readers
def session_events(store: Any, event_type: str | None = None, session_id: str | None = None) -> list[Any]:
    out = []
    for session in store.list_sessions(limit=1000):
        if session_id is not None and session.id != session_id:
            continue
        for event in store.list_events(session_id=session.id, limit=10000):
            if event_type is None or event.event_type == event_type or (
                    event_type.endswith("*") and event.event_type.startswith(event_type[:-1])):
                out.append(event)
    return out


def brain_events(session_api: Any, session_id: str, prefix: str, trace_id: str | None = None) -> list[dict[str, Any]]:
    """Events read back through the brain session API (SessctlAdapter.list_events)."""
    rows = session_api.list_events(session_id, trace_id=trace_id)
    out = []
    for row in rows:
        event_type = str(row.get("event_type") or row.get("type") or "")
        if event_type.startswith(prefix):
            payload = row.get("payload") or row.get("payload_json") or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except ValueError:
                    payload = {"raw": payload}
            out.append({"event_type": event_type, "trace_id": row.get("trace_id"),
                        "llm_call_id": payload.get("llm_call_id"), "reason": payload.get("reason"),
                        "extracted_items": payload.get("extracted_items")})
    return out


def intent_view(event: Any) -> dict[str, Any]:
    p = event.payload
    return {"canonical_event_id": event.canonical_event_id, "capture_id": p.get("capture_id"),
            "capture_state": p.get("capture_state"), "root_turn_id": p.get("root_turn_id"),
            "runtime_session_id": p.get("runtime_session_id"), "agent_id": p.get("agent_id")}


def sqlite_rows(path: Path, sql: str, params: tuple = ()) -> list[tuple] | str:
    import sqlite3
    from contextlib import closing

    if not Path(path).is_file():
        return f"missing: {Path(path).name}"
    try:
        with closing(sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)) as conn:
            return [tuple(r) for r in conn.execute(sql, params).fetchall()]
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


def chdir_env(root: Path) -> dict[str, str | None]:
    keys = ("OPENMINION_HOME", "OPENMINION_DATA_ROOT", "PROBE_420_PROVIDER_KEY")
    old = {k: os.environ.get(k) for k in keys}
    old["__cwd__"] = os.getcwd()
    root.mkdir(parents=True, exist_ok=True)
    os.chdir(root)
    os.environ["OPENMINION_HOME"] = str(root)
    os.environ["OPENMINION_DATA_ROOT"] = str(root / ".openminion")
    os.environ["PROBE_420_PROVIDER_KEY"] = "local-dummy-key"
    return old


def restore_env(old: dict[str, str | None]) -> None:
    os.chdir(str(old.pop("__cwd__")))
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
