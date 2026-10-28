"""Turn memory writes outside the turn's capture, under `NEVER write memory records`.

Installed package only (no repo test fixtures); run with `python -I -X utf8`
from an empty directory outside any checkout. It reuses the #448 probe's
helpers (`probe_420_lib.py`, `_common.py`, unchanged): a real `APIRuntime` for
one agent, `openminion`, whose `openai` provider is a scripted
OpenAI-compatible server on 127.0.0.1, an identity profile with one hard
constraint, and call-through recorders that never change a result. This probe
adds a provider that can make one `memory_write` tool call and answer a memory
consolidation turn, and call-through recorders on the brain's memory API
(`write_record`, `stage_candidate`, `apply_outcome_feedback`) and on every
`memory.write` decision of the agent's security policy.

Cases (each a fresh runtime home):
  W  identity profile with `NEVER write memory records`
  N  identity profile with `NEVER reveal secrets` (control)
  X  no identity profile (control)

Steps in each case (every turn has an approval callback that records and denies):
  T1  SDK `Agent.run`, session s1: "Please remember that I prefer tabs over spaces ..."
  T2  SDK `Agent.run`, session s1: "Which indentation style do I prefer?"
  T3  SDK `Agent(forced_tools=["memory.write"]).run`, session s1: the same question;
      the model calls `memory_write` once; the approval callback denies it
  T4  `APIRuntime.run_turn`, session s2, conversation `chat`: "Say hello."
  T5  `APIRuntime.run_turn`, session s2, conversation `chat`: the question
  C   one memory consolidation run (daemon TaskManager + cron scheduler). The
      scripted model routes the consolidation turn into the act loop (entry
      `decompose` with no subtasks); if the request it then answers contains the
      `tool_outcome` candidate id, it replies with a `memory_consolidation`
      trailer that promotes that candidate (a real model may decide otherwise;
      the probe records what the runtime does with that decision)
  T6  SDK `Agent.run`, session s3: "Say hello."
After T1, the case also asks recovery's gate for T1's capture and the turn's
tool adapter for `memory.write`.

Usage: python -I -X utf8 probe_turn_memory_writes.py <work_dir> <out.json>
"""

from __future__ import annotations

import json
import re
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


info = banner("probe_turn_memory_writes",
              provider="openai (OpenAI-compatible HTTP) -> scripted ThreadingHTTPServer on 127.0.0.1",
              dist_versions={n: _dist(n) for n in ("openminion", "sophiagraph", "pydantic")},
              openminion_direct_url=_direct_url())

from openminion import Agent  # noqa: E402
from openminion.modules.controlplane.constants import CALLER_HANDLES_DELIVERY_METADATA_KEY  # noqa: E402
from openminion.modules.policy.adapters.memory import memory_capture_recovery_allowed  # noqa: E402
from openminion.modules.session.capture import build_capture_identity  # noqa: E402
from openminion.services.agent.execution.composition import AgentServiceTurnFlowAdapter  # noqa: E402

import probe_420_lib as lib  # noqa: E402
from probe_420_lib import (  # noqa: E402
    AGENT_ID,
    CAPTURE_ITEM,
    CAPTURE_MESSAGE,
    Cron,
    Observers,
    ScriptedProvider,
    build_runtime,
    chdir_env,
    intent_view,
    restore_env,
    session_events,
    sqlite_rows,
)

WRITE_CONSTRAINT = "NEVER write memory records"
NEUTRAL_CONSTRAINT = "NEVER reveal secrets"
QUESTION = "Which indentation style do I prefer?"
CONSOLIDATION_MESSAGE = "memory_consolidation trailer"
HELLO = "Say hello."
TOOL_ARGS = {"record_type": "fact", "key": "fact:indentation", "title": "Indentation",
             "content": "The user prefers tabs."}
REFUSED_TEXT = TOOL_ARGS["content"]
# The consolidation preview is cut at 220 characters, inside the refused content.
REFUSED_PREFIX = '"content":"The user prefers'
CANONICAL_ARGS = json.dumps(TOOL_ARGS, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
c = Checks()


# ---------------------------------------------------------------- provider
def _last_user(body: dict[str, Any]) -> str:
    for message in reversed(body.get("messages") or []):
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, list):
                content = " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
            return str(content or "")
    return ""


class TurnProvider(ScriptedProvider):
    """The #448 probe's scripted server, plus one `memory_write` tool call and a
    consolidation reply. Keeps every request's text in memory (not in the output)."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.write_tool_call = False
        self.promote_candidate_id: str | None = None
        self.consolidation_entry_done = False
        super().__init__()

    def respond(self, body: dict[str, Any], path: str) -> dict[str, Any]:
        txt = lib._all_text(body)
        last_user = _last_user(body)
        names = [str((t.get("function") or {}).get("name", "")) for t in body.get("tools") or []
                 if isinstance(t, dict)]
        choice = body.get("tool_choice")
        forced = str((choice.get("function") or {}).get("name", "")) if isinstance(choice, dict) else ""
        with self.lock:
            if forced == "submit_output":
                if "purpose: reflect" in txt:
                    kind = "extraction"
                elif "purpose: judge" in txt or "[JUDGE CONTEXT]" in txt:
                    kind = "judge"
                elif "freshness-routing classifier" in txt:
                    kind = "freshness"
                else:
                    kind = "other_structured"
            elif self.write_tool_call and "memory_write" in names:
                kind = "tool_call"
                self.write_tool_call = False
            elif self.promote_candidate_id and self.promote_candidate_id in txt:
                kind = "consolidation_reply"
            elif (CONSOLIDATION_MESSAGE in last_user and "decompose" in names
                  and not self.consolidation_entry_done):
                # Route the consolidation turn into the act loop, which shows the candidate batch.
                kind = "consolidation_entry"
                self.consolidation_entry_done = True
            elif CONSOLIDATION_MESSAGE in last_user:
                kind = "consolidation_other"
            elif "purpose: decide" in txt:
                kind = "decide"
            else:
                kind = "other_text"
            index = len(self.requests)
            self.requests.append({"index": index, "kind": kind, "path": path, "model": body.get("model"),
                                  "tools": sorted(n for n in names if n), "forced": forced or None,
                                  "decision_memory_section": "[DECISION MEMORY]" in txt,
                                  "consolidation_batch": "[MEMORY CONSOLIDATION CANDIDATES]" in txt,
                                  "t": time.monotonic()})
            self.texts.append(txt)
        if kind == "extraction":
            items = [dict(CAPTURE_ITEM)] if CAPTURE_MESSAGE in txt else []
            return lib._submit({"items": items}, f"call-extract-{index}")
        if kind == "judge":
            return lib._submit({"final_answer": "Noted.", "satisfied": True, "next_action": "close"},
                               f"call-judge-{index}")
        if kind == "freshness":
            return lib._submit({"intent": "statement", "domain": "general", "time_sensitive": False,
                                "needs_live_data": False, "needs_sources": False, "needs_exact_date": False,
                                "answer_mode": "direct", "reason": "No live data needed.", "confidence": 1},
                               f"call-freshness-{index}")
        if kind == "other_structured":
            return lib._submit({}, f"call-other-{index}")
        if kind == "tool_call":
            call = {"id": f"call-memory-write-{index}", "type": "function",
                    "function": {"name": "memory_write", "arguments": json.dumps(TOOL_ARGS)}}
            return lib._completion({"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls")
        if kind == "consolidation_entry":
            call = {"id": f"call-decompose-{index}", "type": "function",
                    "function": {"name": "decompose", "arguments": json.dumps({"subtasks": []})}}
            return lib._completion({"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls")
        if kind == "consolidation_reply":
            signal = {"decisions": [{"candidate_id": self.promote_candidate_id, "action": "promote",
                                     "reasoning": "probe: promote the staged tool outcome"}]}
            return lib._text("Consolidation reviewed.\n<memory_consolidation>" + json.dumps(signal)
                             + "</memory_consolidation>")
        if CAPTURE_MESSAGE in last_user:
            return lib._text("Noted: tabs over spaces.")
        if QUESTION in last_user:
            return lib._text("You prefer tabs.")
        if HELLO in last_user:
            return lib._text("Hello.")
        return lib._text("Consolidation reviewed; nothing to change.")


# ---------------------------------------------------------------- recorders
def _rel(filename: str) -> str:
    return filename.replace("\\", "/").rsplit("/openminion/", 1)[-1]


def _callers(depth: int = 4) -> list[str]:
    frames = [f for f in traceback.extract_stack()[:-2] if "/openminion/" in f.filename.replace("\\", "/")]
    return [f"{_rel(f.filename)}:{f.lineno} {f.name}" for f in reversed(frames[-depth:])]


class Recorder:
    """Call-through recorders: the brain memory API's writes and the policy's memory.write decisions."""

    KEEP = {
        "write_record": ("scope", "record_type", "key", "title", "tags"),
        "stage_candidate": ("scope", "record_type", "title", "content", "tags", "meta"),
        "apply_outcome_feedback": ("record_ids", "outcome", "command_id", "feedback_delta"),
    }

    def __init__(self, memory_api: Any, policy: Any) -> None:
        self.writes: list[dict[str, Any]] = []
        self.decisions: list[dict[str, Any]] = []
        self._undo: list[tuple[Any, str, Any]] = []
        for name in self.KEEP:
            if callable(getattr(memory_api, name, None)):
                self._wrap(memory_api, name)
        if policy is not None:
            original = policy.evaluate

            def evaluate(check: Any, *args: Any, **kwargs: Any) -> Any:
                decision = original(check, *args, **kwargs)
                if getattr(check.action, "tool_name", None) == "memory.write":
                    ctx = check.context
                    self.decisions.append({"channel": ctx.channel, "target": ctx.target,
                                           "session_id": ctx.session_id, "run_id": ctx.run_id,
                                           "decision": decision.decision, "reason_code": decision.reason_code,
                                           "t": time.monotonic()})
                return decision

            policy.evaluate = evaluate
            self._undo.append((policy, "evaluate", original))

    def _wrap(self, api: Any, name: str) -> None:
        original = getattr(api, name)
        keep = self.KEEP[name]

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            row: dict[str, Any] = {"method": name, "callers": _callers(), "t": time.monotonic(),
                                   **{k: kwargs.get(k) for k in keep if k in kwargs}}
            access = kwargs.get("access_context")
            if access is not None:
                row["access_context"] = {"agent_id": getattr(access, "agent_id", None),
                                         "session_id": getattr(access, "session_id", None)}
            try:
                result = original(*args, **kwargs)
                row["result"] = result if isinstance(result, (str, int, float)) else repr(result)
                return result
            except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
                row["raised"] = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                self.writes.append(row)

        setattr(api, name, wrapper)
        self._undo.append((api, name, None))

    def uninstall(self) -> None:
        for obj, name, original in reversed(self._undo):
            try:
                if original is None:
                    delattr(obj, name)
                else:
                    setattr(obj, name, original)
            except AttributeError:
                pass
        self._undo.clear()


class ConsolidationRecorder:
    """Call-through recorders on the consolidation signal parser and the decision applier."""

    def __init__(self) -> None:
        import openminion.modules.brain.loop.adaptive.modes as modes
        import openminion.modules.brain.loop.tools.runtime as loop_runtime

        self.parsed: list[dict[str, Any]] = []
        self.applied: list[dict[str, Any]] = []
        self._undo = [(modes, "apply_memory_consolidation_decisions",
                       modes.apply_memory_consolidation_decisions),
                      (loop_runtime, "_normalize_memory_consolidation_response",
                       loop_runtime._normalize_memory_consolidation_response)]
        parse, apply = self._undo[1][2], self._undo[0][2]

        def parse_rec(response: Any) -> Any:
            out = parse(response)
            text = str(getattr(response, "output_text", "") or "")
            if "<memory_consolidation>" in text:
                self.parsed.append({"signal": getattr(out, "memory_consolidation", None)})
            return out

        def apply_rec(*args: Any, **kwargs: Any) -> Any:
            row: dict[str, Any] = {k: kwargs.get(k) for k in ("decisions", "target_scope", "selected_candidate_ids")}
            try:
                result = apply(*args, **kwargs)
                row["result"] = result
                return result
            except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
                row["raised"] = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                self.applied.append(row)

        modes.apply_memory_consolidation_decisions = apply_rec
        loop_runtime._normalize_memory_consolidation_response = parse_rec

    def uninstall(self) -> None:
        for module, name, original in self._undo:
            setattr(module, name, original)


# ---------------------------------------------------------------- store
def _db_path(obs: Any) -> Path | None:
    store = obs.assembly.service._store
    inner = getattr(store, "_store", None)
    return Path(str(inner.db_path)) if inner is not None and getattr(inner, "db_path", None) else None


def _parse(value: Any) -> Any:
    try:
        return json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return value


RECORD_COLS = ("id", "scope", "type", "key", "title", "content_json", "tags_json", "source", "confidence",
               "meta_json", "tier", "created_at", "updated_at", "is_deleted")
CANDIDATE_COLS = ("candidate_id", "session_id", "proposed_scope", "type", "key", "title", "content_json",
                  "tags_json", "source", "confidence", "meta_json", "status", "created_at", "updated_at")


def snapshot(db: Path | None) -> dict[str, dict[str, dict[str, Any]]]:
    if db is None:
        return {"records": {}, "candidates": {}}
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for table, cols, key in (("memory_records", RECORD_COLS, "id"),
                             ("memory_candidates", CANDIDATE_COLS, "candidate_id")):
        rows = sqlite_rows(db, f"SELECT {', '.join(cols)} FROM {table} ORDER BY created_at, {key}")
        name = "records" if table == "memory_records" else "candidates"
        out[name] = {}
        if isinstance(rows, str):
            out[name] = {"error": {"error": rows}}
            continue
        for row in rows:
            item = {col: (_parse(val) if col.endswith("_json") else val) for col, val in zip(cols, row)}
            out[name][str(item[key])] = item
    return out


def diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in ("records", "candidates"):
        old, new = before.get(name, {}), after.get(name, {})
        out[f"new_{name}"] = [new[k] for k in new if k not in old]
        changed = []
        for k in new:
            if k in old and new[k] != old[k]:
                changed.append({"id": k, "fields": {f: [old[k].get(f), new[k].get(f)] for f in new[k]
                                                    if new[k].get(f) != old[k].get(f)}})
        out[f"changed_{name}"] = changed
    return out


# ---------------------------------------------------------------- helpers
def id_hits(provider: TurnProvider, start: int, end: int, needle: str) -> list[dict[str, Any]]:
    """Requests in [start, end) whose text contains `needle`, with that line and the next two."""
    hits = []
    for index in range(start, end):
        text = provider.texts[index]
        if needle and needle in text:
            match = re.search(r"[^\n]{0,80}" + re.escape(needle) + r"[^\n]{0,240}(?:\n[^\n]{0,240}){0,2}", text)
            hits.append({"request": index, "kind": provider.requests[index]["kind"],
                         "line": match.group(0) if match else None})
    return hits


def sdk_turn(ctx: dict[str, Any], label: str, message: str, session_id: str,
             forced_tools: list[str] | None = None, conversation_id: str | None = None) -> dict[str, Any]:
    """One turn: `Agent.run`, or `APIRuntime.run_turn` with an explicit conversation id."""
    provider, rec, obs, runtime = ctx["provider"], ctx["rec"], ctx["obs"], ctx["runtime"]
    db = ctx["db"]
    before = snapshot(db)
    mark_r, mark_w, mark_d = len(provider.requests), len(rec.writes), len(rec.decisions)
    approvals: list[dict[str, Any]] = []

    def approval(*a: Any) -> bool:
        approvals.append({"tool": str(a[0]) if a else None, "args": a[1] if len(a) > 1 else None})
        return False

    if forced_tools:
        provider.write_tool_call = True
    t0 = time.monotonic()
    if conversation_id is None:
        entry = "Agent.run"
        with Agent(runtime=runtime, forced_tools=forced_tools) as agent:
            res = agent.run(message, session_id=session_id, timeout_seconds=120, approval_callback=approval)
        run_state, text, md = res.run_state, res.text, dict(res.metadata or {})
    else:
        # The payload Agent.run builds, plus the conversation id an interactive chat keeps.
        entry = "APIRuntime.run_turn"
        raw = runtime.run_turn(payload={"message": message, "session_id": session_id, "deliver": False,
                                        "inbound_metadata": {CALLER_HANDLES_DELIVERY_METADATA_KEY: "true"},
                                        "conversation_id": conversation_id, "timeout_seconds": 120},
                               approval_callback=approval)
        run_state, text, md = raw.get("run_state"), raw.get("body"), dict(raw.get("metadata") or {})
    t1 = time.monotonic()
    provider.write_tool_call = False
    intents = [intent_view(e) for e in session_events(runtime.sessions, "turn.outcome", session_id)]
    return {
        "label": label, "entry": entry, "session_id": session_id, "conversation_id": conversation_id,
        "message": message, "forced_tools": forced_tools, "run_state": run_state, "text": text,
        "memory_capture_state": md.get("memory_capture_state"),
        "memory_capture_bundle_result": _parse(md.get("memory_capture_bundle_result")),
        "intent": intents[-1] if intents else None,
        "provider_requests": [r["kind"] for r in provider.requests[mark_r:]],
        "request_range": [mark_r, len(provider.requests)],
        "approval_callback_calls": approvals,
        "memory_write_decisions": [{k: v for k, v in d.items() if k != "t"} for d in rec.decisions[mark_d:]
                                   if t0 <= d["t"] <= t1],
        "memory_api_writes": [{k: v for k, v in w.items() if k != "t"} for w in rec.writes[mark_w:]],
        "store": diff(before, snapshot(db)),
    }


def consolidation(ctx: dict[str, Any], job_id: str) -> dict[str, Any]:
    provider, rec, obs, cron, db = ctx["provider"], ctx["rec"], ctx["obs"], ctx["cron"], ctx["db"]
    before = snapshot(db)
    marks = {k: len(getattr(obs, k)) for k in ("recoveries", "extractor_calls", "submitted_turns")}
    mark_r, mark_w, mark_d = len(provider.requests), len(rec.writes), len(rec.decisions)
    queued = cron.run_now(job_id)
    done = cron.wait(job_id, str(queued.get("run_id")))
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not any(
            e.get("event") == "cron.run.finished" and e.get("run_id") == queued.get("run_id")
            for e in obs.cron_events):
        time.sleep(0.05)
    end_r = len(provider.requests)
    return {
        "state": done.get("state"), "error": done.get("error"), "summary": done.get("summary"),
        "recovery": [r.get("result") for r in obs.recoveries[marks["recoveries"]:]],
        "extractor_calls": len(obs.extractor_calls) - marks["extractor_calls"],
        "submitted_turn_sessions": [t.get("session_id") for t in obs.submitted_turns[marks["submitted_turns"]:]],
        "provider_requests": [r["kind"] for r in provider.requests[mark_r:end_r]],
        "request_range": [mark_r, end_r],
        "memory_write_decisions": [{k: v for k, v in d.items() if k != "t"} for d in rec.decisions[mark_d:]],
        "memory_api_writes": [{k: v for k, v in w.items() if k != "t"} for w in rec.writes[mark_w:]],
        "store": diff(before, snapshot(db)),
    }


def tool_adapter_check(runtime: Any, runner: Any) -> dict[str, Any]:
    try:
        decision = runner.tool_api.policy_adapter.evaluate(
            tool_name="memory.write", tool_spec=runtime.tools.get("memory.write"), args={})
        return {"allowed": bool(decision.allowed), "requires_confirm": bool(getattr(decision, "requires_confirm",
                                                                                    False)),
                "reason": getattr(decision, "reason", None)}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def run_case(label: str, constraint: str | None) -> dict[str, Any]:
    root = WORK / label
    provider = TurnProvider()
    old_env = chdir_env(root)
    row: dict[str, Any] = {"case": label, "constraint": constraint}
    runtime = obs = rec = cron = crec = None
    try:
        runtime = build_runtime(root, provider, identity_constraint=constraint)
        obs = Observers(runtime).install()
        runner = obs.service._get_runner()
        memory_api = runner.memory_api
        row["runner_memory_api_is_assembly_memctl"] = memory_api is obs.memctl
        row["identity_constraints"] = list(getattr(obs.security_policy, "_identity_constraints", []) or [])
        row["security_policy_present"] = obs.security_policy is not None
        rec = Recorder(memory_api, obs.security_policy)
        crec = ConsolidationRecorder()
        ctx = {"provider": provider, "rec": rec, "obs": obs, "runtime": runtime, "db": _db_path(obs)}
        row["memory_db"] = ctx["db"].name if ctx["db"] is not None else None
        turns = row["turns"] = {}
        turns["T1"] = sdk_turn(ctx, "T1", CAPTURE_MESSAGE, "s1")
        intent = turns["T1"]["intent"] or {}
        identity = build_capture_identity(runtime_session_id="s1", root_turn_id=str(intent.get("root_turn_id")))
        port = AgentServiceTurnFlowAdapter(obs.service)
        mark_d = len(rec.decisions)
        row["recovery_gate_T1"] = {
            "returned": memory_capture_recovery_allowed(
                "s1", identity.root_turn_id, identity.event_id, identity.capture_id,
                policy=port.security_policy, tools=port.tools, agent_id=port.identity_agent_id),
            "decisions": [{k: v for k, v in d.items() if k != "t"} for d in rec.decisions[mark_d:]]}
        row["tool_adapter_memory_write"] = tool_adapter_check(runtime, runner)
        turns["T2"] = sdk_turn(ctx, "T2", QUESTION, "s1")
        turns["T3"] = sdk_turn(ctx, "T3", QUESTION, "s1", forced_tools=["memory.write"])
        turns["T4"] = sdk_turn(ctx, "T4", HELLO, "s2", conversation_id="chat")
        turns["T5"] = sdk_turn(ctx, "T5", QUESTION, "s2", conversation_id="chat")
        tool_outcomes = [cand for cand in snapshot(ctx["db"])["candidates"].values()
                         if cand.get("type") == "tool_outcome"]
        row["tool_outcome_candidate_ids"] = [cand["candidate_id"] for cand in tool_outcomes]
        cron = ctx["cron"] = Cron(runtime, root)
        created = cron.create_consolidation_job(batch_limit=8)
        row["job_id"] = str(created.get("task_id"))
        cron.start()
        provider.promote_candidate_id = tool_outcomes[0]["candidate_id"] if tool_outcomes else None
        row["consolidation"] = consolidation(ctx, row["job_id"])
        row["consolidation"]["signal_parsed"] = list(crec.parsed)
        row["consolidation"]["decisions_applied"] = list(crec.applied)
        provider.promote_candidate_id = None
        turns["T6"] = sdk_turn(ctx, "T6", HELLO, "s3")
        row["final_store"] = snapshot(ctx["db"])
        # Read-back: where do the stored ids and the refused content appear in later requests?
        decisions = {t: [r["id"] for r in turns[t]["store"]["new_records"] if r.get("type") == "decision"]
                     for t in turns}
        promoted = [r["id"] for r in row["consolidation"]["store"]["new_records"] if r.get("type") == "tool_outcome"]
        row["request_log"] = [{k: v for k, v in r.items() if k not in ("t", "path", "model")}
                              for r in provider.requests]
        readback: dict[str, Any] = {}
        a, b = turns["T2"]["request_range"]
        readback["T2_T1_decision_ids"] = [h for d in decisions["T1"] for h in id_hits(provider, a, b, d)]
        a, b = turns["T5"]["request_range"]
        readback["T5_T4_decision_ids"] = [h for d in decisions["T4"] for h in id_hits(provider, a, b, d)]
        readback["T5_s1_decision_ids"] = [h for t in ("T1", "T2", "T3") for d in decisions[t]
                                          for h in id_hits(provider, a, b, d)]
        a, b = row["consolidation"]["request_range"]
        readback["C_candidate_id"] = [h for d in row["tool_outcome_candidate_ids"] for h in id_hits(provider, a, b, d)]
        readback["C_refused_text"] = id_hits(provider, a, b, REFUSED_PREFIX)
        a, b = turns["T6"]["request_range"]
        readback["T6_promoted_ids"] = [h for d in promoted for h in id_hits(provider, a, b, d)]
        readback["T6_refused_text"] = id_hits(provider, a, b, REFUSED_TEXT)
        readback["T6_candidate_id"] = [h for d in row["tool_outcome_candidate_ids"] for h in id_hits(provider, a, b, d)]
        later = [turns[t]["request_range"] for t in ("T4", "T5", "T6")]
        readback["T4_T6_candidate_or_refused_text"] = [
            h for a2, b2 in later for needle in [*row["tool_outcome_candidate_ids"], REFUSED_TEXT, TOOL_ARGS["key"]]
            for h in id_hits(provider, a2, b2, needle)]
        row["readback"] = readback
        row["all_outcome_feedback_calls"] = [w for w in rec.writes if w["method"] == "apply_outcome_feedback"]
        row["provider_hosts"] = sorted({provider.base_url.split("/")[2].split(":")[0]})
    except Exception as exc:  # noqa: BLE001 - recorded; the checks then fail
        row["case_error"] = f"{type(exc).__name__}: {exc}"
        row["case_error_frames"] = [f"{Path(f.filename).name}:{f.lineno} {f.name}"
                                    for f in traceback.extract_tb(exc.__traceback__)][-8:]
    finally:
        if cron is not None:
            try:
                cron.stop()
            except Exception as exc:  # noqa: BLE001
                row["scheduler_stop"] = f"{type(exc).__name__}: {exc}"
        if crec is not None:
            crec.uninstall()
        if rec is not None:
            rec.uninstall()
        if obs is not None:
            obs.uninstall()
        if runtime is not None:
            try:
                runtime.close()
            except Exception as exc:  # noqa: BLE001
                row["runtime_close"] = f"{type(exc).__name__}: {exc}"
        restore_env(old_env)
        provider.close()
    return row


cases = {"W": run_case("W", WRITE_CONSTRAINT), "N": run_case("N", NEUTRAL_CONSTRAINT), "X": run_case("X", None)}
observed = {"cases": cases}
TURNS = ("T1", "T2", "T3", "T4", "T5", "T6")
TOOL_OUTCOME_TITLE = "tool_outcome:memory.write:policy_denied"


def decision_facts(case: dict[str, Any]) -> dict[str, Any]:
    """Per turn: the new `decision` records, the calls that wrote them, and the memory.write targets asked."""
    out: dict[str, Any] = {}
    for t in TURNS:
        turn = (case.get("turns") or {}).get(t) or {}
        recs = [r for r in (turn.get("store") or {}).get("new_records", []) if r.get("type") == "decision"]
        calls = [w for w in turn.get("memory_api_writes", [])
                 if w.get("method") == "write_record" and w.get("record_type") == "decision"]
        out[t] = {
            "run_state": turn.get("run_state"),
            "records": [{"id": r.get("id"), "scope": r.get("scope"), "title": r.get("title"),
                         "route": (r.get("content_json") or {}).get("route_chosen")} for r in recs],
            "writer": [" <- ".join(w.get("callers", [])[:2]) for w in calls],
            "memory_write_targets": sorted({d.get("target") for d in turn.get("memory_write_decisions", [])}),
        }
    return out


def decisions_ok(case: dict[str, Any]) -> bool:
    facts = decision_facts(case)
    for t, f in facts.items():
        sid = ((case.get("turns") or {}).get(t) or {}).get("session_id")
        if f["run_state"] != "completed" or len(f["records"]) != 1 or len(f["writer"]) != 1:
            return False
        rec = f["records"][0]
        if not str(rec["scope"]).startswith(f"session:{sid}::conv:") or rec["title"] != f"Decision: {rec['route']}":
            return False
        if "write_decision_memory" not in f["writer"][0] or not set(f["memory_write_targets"]) <= {"memory-capture"}:
            return False
    return True


def tool_outcome_facts(case: dict[str, Any]) -> dict[str, Any]:
    turn = (case.get("turns") or {}).get("T3") or {}
    cands = [r for r in (turn.get("store") or {}).get("new_candidates", []) if r.get("type") == "tool_outcome"]
    calls = [w for w in turn.get("memory_api_writes", []) if w.get("method") == "stage_candidate"]
    final = case.get("final_store") or {}
    return {
        "approvals": turn.get("approval_callback_calls"),
        "text": turn.get("text"),
        "candidates": [{k: r.get(k) for k in ("candidate_id", "proposed_scope", "status", "title", "content_json",
                                               "meta_json", "session_id", "source", "confidence")} for r in cands],
        "writer": [" <- ".join(w.get("callers", [])[:2]) for w in calls],
        "memory_write_targets": sorted({d.get("target") for d in turn.get("memory_write_decisions", [])}),
        "fact_records_with_tool_key": [r.get("id") for r in (final.get("records") or {}).values()
                                       if r.get("key") == TOOL_ARGS["key"]],
    }


def tool_outcome_ok(case: dict[str, Any]) -> bool:
    f = tool_outcome_facts(case)
    if f["approvals"] != [{"tool": "memory.write", "args": TOOL_ARGS}] or f["fact_records_with_tool_key"]:
        return False
    if len(f["candidates"]) != 1 or len(f["writer"]) != 1 or "_stage_tool_outcome_candidate" not in f["writer"][0]:
        return False
    cand = f["candidates"][0]
    content, meta = cand.get("content_json") or {}, cand.get("meta_json") or {}
    return (cand.get("proposed_scope") == f"agent:{AGENT_ID}" and cand.get("status") == "proposed"
            and cand.get("title") == TOOL_OUTCOME_TITLE and content.get("outcome") == "policy_denied"
            and content.get("args_signature") == CANONICAL_ARGS
            and meta.get("source_args_signature") == CANONICAL_ARGS
            and set(f["memory_write_targets"]) <= {"memory-capture"})


def shape(case: dict[str, Any]) -> dict[str, Any]:
    """What lands, without ids and timestamps: compared across W, N and X."""
    facts = decision_facts(case)
    tf = tool_outcome_facts(case)
    cand = (tf["candidates"] or [{}])[0]
    return {
        "decisions": {t: [(r["title"], str(r["scope"]).split("::conv:")[0]) for r in f["records"]]
                      for t, f in facts.items()},
        "tool_outcome": {"scope": cand.get("proposed_scope"), "status": cand.get("status"),
                         "title": cand.get("title"),
                         "content": {k: v for k, v in (cand.get("content_json") or {}).items() if k != "turn_index"}},
    }


w, n, x = cases["W"], cases["N"], cases["X"]
rg = w.get("recovery_gate_T1") or {}
c.check("W. the identity profile is loaded, and memory.write is refused for the agent: recovery's gate for T1's "
        "capture returns False (require_approval, identity_hard_constraint_restricted) and the turn's tool adapter "
        "does not allow memory.write (requires confirmation, identity_hard_constraint_restricted)",
        w.get("identity_constraints") == [WRITE_CONSTRAINT] and rg.get("returned") is False
        and [(d.get("decision"), d.get("reason_code")) for d in rg.get("decisions", [])]
        == [("require_approval", "identity_hard_constraint_restricted")]
        and w.get("tool_adapter_memory_write") == {"allowed": False, "requires_confirm": True,
                                                    "reason": "identity_hard_constraint_restricted"},
        {"constraints": w.get("identity_constraints"), "recovery_gate": rg,
         "tool_adapter": w.get("tool_adapter_memory_write")})
c.check("W. each of the six turns writes one `decision` record to memory_records (scope session:<session>::conv:<id>, "
        "title `Decision: <route>`) through the brain memory API's write_record from write_decision_memory; no "
        "memory.write decision is asked for it (the only target asked in a turn, if any, is the turn capture's "
        "`memory-capture`)",
        decisions_ok(w), decision_facts(w))
c.check("W. T3: the model's memory.write call asks the approval callback once (denied) and writes no record; one "
        "`tool_outcome` candidate is staged in memory_candidates through stage_candidate from "
        "_stage_tool_outcome_candidate: scope agent:openminion, status proposed, title "
        f"{TOOL_OUTCOME_TITLE}, content.outcome policy_denied, content.args_signature and "
        "meta.source_args_signature = the refused call's arguments; no memory.write decision is asked for it",
        tool_outcome_ok(w), tool_outcome_facts(w))
rb = w.get("readback") or {}
c.check("W. read-back by id: within one conversation (T4 -> T5, APIRuntime.run_turn with conversation `chat`) the next "
        "turn's requests carry T4's decision record id; T2 (a new Agent.run conversation) carries no T1 decision id, "
        "and T5 carries no s1 decision id",
        bool(rb.get("T5_T4_decision_ids")) and not rb.get("T2_T1_decision_ids") and not rb.get("T5_s1_decision_ids"),
        {k: rb.get(k) for k in ("T5_T4_decision_ids", "T2_T1_decision_ids", "T5_s1_decision_ids")})
feedback = {k: case.get("all_outcome_feedback_calls") for k, case in cases.items()}
c.check("W/N/X. no apply_outcome_feedback call in any turn or consolidation run (no built-in tool result carries "
        "memory_use_refs, and the scripted judge cites none)",
        all(v == [] for v in feedback.values()), feedback)
for label, case, constraint in (("N", n, [NEUTRAL_CONSTRAINT]), ("X", x, [])):
    crg = case.get("recovery_gate_T1") or {}
    c.check(f"{label}. control ({'NEVER reveal secrets' if label == 'N' else 'no identity profile'}): memory.write is "
            "allowed (recovery's gate True, tool adapter allows), and the same writes land: one decision record per "
            "turn and the same tool_outcome candidate for the denied memory.write call",
            case.get("identity_constraints") == constraint and crg.get("returned") is True
            and (case.get("tool_adapter_memory_write") or {}).get("allowed") is True
            and decisions_ok(case) and tool_outcome_ok(case) and shape(case) == shape(w),
            {"constraints": case.get("identity_constraints"), "recovery_gate": crg.get("returned"),
             "tool_adapter": case.get("tool_adapter_memory_write"), "shape_equals_W": shape(case) == shape(w)})


def consolidation_facts(case: dict[str, Any]) -> dict[str, Any]:
    cons = case.get("consolidation") or {}
    a, b = cons.get("request_range") or [0, 0]
    log = (case.get("request_log") or [])[a:b]
    rb = case.get("readback") or {}
    ids = case.get("tool_outcome_candidate_ids") or []
    store = cons.get("store") or {}
    return {
        "state": cons.get("state"), "recovery": cons.get("recovery"),
        "requests": [r.get("kind") for r in log],
        "batch_requests": [r.get("index") for r in log if r.get("consolidation_batch")],
        "candidate_id_in": [[h.get("request"), h.get("kind")] for h in rb.get("C_candidate_id", [])],
        "refused_text_line": [h.get("line") for h in rb.get("C_refused_text", [])],
        "candidate_status": [[c.get("id"), c["fields"].get("status")] for c in store.get("changed_candidates", [])
                             if c.get("id") in ids],
        "new_tool_outcome_records": [{k: r.get(k) for k in ("id", "scope", "type", "title", "content_json", "source")}
                                     for r in store.get("new_records", []) if r.get("type") == "tool_outcome"],
        "memory_write_targets": sorted({d.get("target") for d in cons.get("memory_write_decisions", [])}),
        "signal_parsed": cons.get("signal_parsed"), "decisions_applied": cons.get("decisions_applied"),
        "T6_promoted_or_refused_text": rb.get("T6_promoted_ids", []) + rb.get("T6_refused_text", []),
        "T4_T6_candidate_or_refused_text": rb.get("T4_T6_candidate_or_refused_text"),
        "final_candidate_status": [((case.get("final_store") or {}).get("candidates") or {}).get(i, {}).get("status")
                                   for i in ids],
        "final_tool_outcome_records": [r.get("id") for r in ((case.get("final_store") or {}).get("records") or {}).values()
                                       if r.get("type") == "tool_outcome"],
    }


cf = consolidation_facts(w)
cand_content = ((tool_outcome_facts(w)["candidates"] or [{}])[0]).get("content_json")
c.check("W. C: the consolidation turn's act-loop request lists the agent's proposed candidates "
        "([MEMORY CONSOLIDATION CANDIDATES]); the `tool_outcome` candidate is there with its id, and its preview "
        "holds the start of the refused content",
        bool(cf["batch_requests"]) and any(k == "consolidation_reply" for _, k in cf["candidate_id_in"])
        and any(REFUSED_PREFIX in str(line) for line in cf["refused_text_line"]),
        {k: cf[k] for k in ("requests", "batch_requests", "candidate_id_in", "refused_text_line")})
applied = cf.get("decisions_applied") or [{}]
apply_result = applied[0].get("result") or {}
c.check("W. C: the scripted `promote` decision is parsed and applied to the selected batch, and the promotion policy "
        "refuses it (`Source 'agent_inferred' requires explicit approval for auto-promotion`): the candidate stays "
        "`proposed` and no `tool_outcome` record is written; the only memory.write decisions in the run, if any, "
        "are recovery's (`memory-capture-recovery`)",
        cf["state"] == "finished" and len(cf.get("decisions_applied") or []) == 1
        and [d.get("candidate_id") for d in applied[0].get("decisions") or []] == w.get("tool_outcome_candidate_ids")
        and set(w.get("tool_outcome_candidate_ids") or ["?"]) <= set(applied[0].get("selected_candidate_ids") or [])
        and apply_result.get("promoted_count") == 0
        and any("requires explicit approval for auto-promotion" in str(e) for e in apply_result.get("errors") or [])
        and cf["final_candidate_status"] == ["proposed"] and cf["final_tool_outcome_records"] == []
        and set(cf["memory_write_targets"]) <= {"memory-capture-recovery"},
        {k: cf[k] for k in ("state", "recovery", "signal_parsed", "decisions_applied", "final_candidate_status",
                            "final_tool_outcome_records", "memory_write_targets")})
c.check("W. the candidate is not shown to later ordinary turns: no T4, T5 or T6 request (sessions s2 and s3) holds its "
        "id, the refused content or the refused key",
        not cf.get("T4_T6_candidate_or_refused_text") and bool(w.get("tool_outcome_candidate_ids")),
        cf.get("T4_T6_candidate_or_refused_text"))
observed["consolidation_W"] = cf
hosts = sorted({h for case in cases.values() for h in case.get("provider_hosts", [])})
errors = {k: case.get("case_error") for k, case in cases.items() if case.get("case_error")}
c.check("Offline provider and no case error: every case's openai base URL is the scripted server on 127.0.0.1",
        hosts == ["127.0.0.1"] and not errors, {"hosts": hosts, "errors": errors})
observed["shape_W"] = shape(w)

write_result(OUT, {"info": info, "summary": c.summary(), "all_pass": c.all_pass, "checks": c.items,
                   "observed": observed})
summary = c.summary()
print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
sys.exit(0 if c.all_pass else 1)
