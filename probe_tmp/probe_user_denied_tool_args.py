"""A tool call the user denies leaves its arguments in a `tool_outcome` candidate.

Installed package only (no repo test fixtures); run with `python -I -X utf8`
from an empty directory outside any checkout. It reuses the #448 probe's
helpers (`probe_420_lib.py`, `_common.py`, unchanged): a real `APIRuntime` for
one agent, `openminion`, whose `openai` provider is a scripted
OpenAI-compatible server on 127.0.0.1, an optional identity profile with one
hard constraint, and call-through recorders that never change a result.

The memory policy allows `memory.write` in cases N and X; W is a reference
under `NEVER write memory records` (openminion#496).
  N  identity profile with `NEVER reveal secrets`
  X  no identity profile
  W  identity profile with `NEVER write memory records` (reference only)

Steps in each case (a fresh runtime home; each step its own session). In every
step the scripted model calls `memory_write` once with the same arguments:
  D  `Agent(forced_tools=["memory.write"]).run(..., approval_callback=deny)`
  A  the same with a callback that approves
  P  the same without an approval callback
Then it lists the agent's proposed candidates as the memory consolidation
context does (`collect_memory_consolidation_candidates`, agent scope).

Usage: python -I -X utf8 probe_user_denied_tool_args.py <work_dir> <out.json>
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


info = banner("probe_user_denied_tool_args",
              provider="openai (OpenAI-compatible HTTP) -> scripted ThreadingHTTPServer on 127.0.0.1",
              dist_versions={n: _dist(n) for n in ("openminion", "sophiagraph", "pydantic")},
              openminion_direct_url=_direct_url())

from openminion import Agent  # noqa: E402

import probe_420_lib as lib  # noqa: E402
from probe_420_lib import (  # noqa: E402
    AGENT_ID,
    Observers,
    ScriptedProvider,
    build_runtime,
    chdir_env,
    restore_env,
    sqlite_rows,
)

WRITE_CONSTRAINT = "NEVER write memory records"
NEUTRAL_CONSTRAINT = "NEVER reveal secrets"
QUESTION = "Which indentation style do I prefer?"
TOOL_ARGS = {"record_type": "fact", "key": "fact:indentation", "title": "Indentation",
             "content": "The user prefers tabs."}
CANONICAL_ARGS = json.dumps(TOOL_ARGS, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
TITLE = "tool_outcome:memory.write:policy_denied"
c = Checks()


class ToolCallProvider(ScriptedProvider):
    """The #448 probe's scripted server, plus one `memory_write` call per armed step."""

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
                self.requests.append({"index": index, "kind": "tool_call", "path": path,
                                      "model": body.get("model"), "t": time.monotonic()})
        if call:
            tool_call = {"id": f"call-memory-write-{index}", "type": "function",
                         "function": {"name": "memory_write", "arguments": json.dumps(TOOL_ARGS)}}
            return lib._completion({"role": "assistant", "content": None, "tool_calls": [tool_call]},
                                   "tool_calls")
        return super().respond(body, path)


def _callers(depth: int = 4) -> list[str]:
    frames = [f for f in traceback.extract_stack()[:-2] if "/openminion/" in f.filename.replace("\\", "/")]
    return [f"{f.filename.replace(chr(92), '/').rsplit('/openminion/', 1)[-1]}:{f.lineno} {f.name}"
            for f in reversed(frames[-depth:])]


class Recorder:
    """Call-through recorders on the brain memory API's stage_candidate and the policy's memory.write decisions."""

    def __init__(self, memory_api: Any, policy: Any) -> None:
        self.stages: list[dict[str, Any]] = []
        self.decisions: list[dict[str, Any]] = []
        self._undo: list[tuple[Any, str, Any]] = []
        stage = memory_api.stage_candidate

        def stage_candidate(*args: Any, **kwargs: Any) -> Any:
            row = {"callers": _callers(), "record_type": kwargs.get("record_type"), "title": kwargs.get("title")}
            result = stage(*args, **kwargs)
            row["result"] = result if isinstance(result, str) else repr(result)
            self.stages.append(row)
            return result

        memory_api.stage_candidate = stage_candidate
        self._undo.append((memory_api, "stage_candidate", None))
        if policy is not None:
            original = policy.evaluate

            def evaluate(check: Any, *args: Any, **kwargs: Any) -> Any:
                decision = original(check, *args, **kwargs)
                if getattr(check.action, "tool_name", None) == "memory.write":
                    self.decisions.append({"target": check.context.target, "decision": decision.decision,
                                           "reason_code": decision.reason_code})
                return decision

            policy.evaluate = evaluate
            self._undo.append((policy, "evaluate", original))

    def uninstall(self) -> None:
        for obj, name, original in reversed(self._undo):
            try:
                if original is None:
                    delattr(obj, name)
                else:
                    setattr(obj, name, original)
            except AttributeError:
                pass


def _db_path(obs: Any) -> Path | None:
    inner = getattr(obs.assembly.service._store, "_store", None)
    return Path(str(inner.db_path)) if inner is not None and getattr(inner, "db_path", None) else None


def _parse(value: Any) -> Any:
    try:
        return json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return value


def rows(db: Path | None) -> dict[str, list[dict[str, Any]]]:
    if db is None:
        return {"tool_records": [], "tool_outcomes": []}
    records = sqlite_rows(db, "SELECT id, type, key, title FROM memory_records WHERE key = ? AND is_deleted = 0",
                          (TOOL_ARGS["key"],))
    candidates = sqlite_rows(
        db, "SELECT candidate_id, session_id, proposed_scope, type, title, content_json, meta_json, source, "
            "confidence, status FROM memory_candidates WHERE type = 'tool_outcome' ORDER BY created_at, candidate_id")
    if isinstance(records, str) or isinstance(candidates, str):
        return {"error": [{"records": records, "candidates": candidates}]}
    cols = ("candidate_id", "session_id", "proposed_scope", "type", "title", "content_json", "meta_json",
            "source", "confidence", "status")
    return {
        "tool_records": [dict(zip(("id", "type", "key", "title"), r)) for r in records],
        "tool_outcomes": [{k: (_parse(v) if k.endswith("_json") else v) for k, v in zip(cols, r)} for r in candidates],
    }


def step(ctx: dict[str, Any], label: str, approve: bool | None) -> dict[str, Any]:
    provider, rec, runtime, db = ctx["provider"], ctx["rec"], ctx["runtime"], ctx["db"]
    before = rows(db)
    mark_s, mark_d, mark_r = len(rec.stages), len(rec.decisions), len(provider.requests)
    approvals: list[dict[str, Any]] = []

    def callback(*a: Any) -> bool:
        approvals.append({"tool": str(a[0]) if a else None, "args": a[1] if len(a) > 1 else None})
        return bool(approve)

    provider.write_tool_call = True
    kwargs: dict[str, Any] = {"session_id": f"step-{label}", "timeout_seconds": 120}
    if approve is not None:
        kwargs["approval_callback"] = callback
    try:
        with Agent(runtime=runtime, forced_tools=["memory.write"]) as agent:
            res = agent.run(QUESTION, **kwargs)
        outcome = {"run_state": res.run_state, "text": res.text}
    except Exception as exc:  # noqa: BLE001 - recorded
        outcome = {"error": f"{type(exc).__name__}: {exc}"}
    provider.write_tool_call = False
    after = rows(db)
    old_ids = {r["candidate_id"] for r in before.get("tool_outcomes", [])}
    old_records = {r["id"] for r in before.get("tool_records", [])}
    return {
        "label": label, "approval_callback": None if approve is None else ("approves" if approve else "denies"),
        **outcome,
        "provider_requests": [r["kind"] for r in provider.requests[mark_r:]],
        "approval_callback_calls": approvals,
        "memory_write_decisions": rec.decisions[mark_d:],
        "stage_candidate_calls": rec.stages[mark_s:],
        "new_tool_records": [r for r in after.get("tool_records", []) if r["id"] not in old_records],
        "new_tool_outcomes": [r for r in after.get("tool_outcomes", []) if r["candidate_id"] not in old_ids],
    }


def consolidation_batch(obs: Any) -> Any:
    try:
        from openminion.modules.memory.runtime.consolidation.extract import collect_memory_consolidation_candidates
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}
    try:
        return collect_memory_consolidation_candidates(obs.memctl, proposed_scope=f"agent:{AGENT_ID}", limit=8)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def run_case(label: str, constraint: str | None) -> dict[str, Any]:
    root = WORK / label
    provider = ToolCallProvider()
    old_env = chdir_env(root)
    row: dict[str, Any] = {"case": label, "constraint": constraint}
    runtime = obs = rec = None
    try:
        runtime = build_runtime(root, provider, identity_constraint=constraint)
        obs = Observers(runtime)
        runner = obs.service._get_runner()
        rec = Recorder(runner.memory_api, obs.security_policy)
        row["identity_constraints"] = list(getattr(obs.security_policy, "_identity_constraints", []) or [])
        ctx = {"provider": provider, "rec": rec, "runtime": runtime, "db": _db_path(obs)}
        row["steps"] = {name: step(ctx, name, approve) for name, approve in (("D", False), ("A", True), ("P", None))}
        row["consolidation_batch"] = consolidation_batch(obs)
        row["final"] = rows(ctx["db"])
        row["provider_hosts"] = sorted({provider.base_url.split("/")[2].split(":")[0]})
    except Exception as exc:  # noqa: BLE001 - recorded; the checks then fail
        row["case_error"] = f"{type(exc).__name__}: {exc}"
        row["case_error_frames"] = [f"{Path(f.filename).name}:{f.lineno} {f.name}"
                                    for f in traceback.extract_tb(exc.__traceback__)][-8:]
    finally:
        if rec is not None:
            rec.uninstall()
        if runtime is not None:
            try:
                runtime.close()
            except Exception as exc:  # noqa: BLE001
                row["runtime_close"] = f"{type(exc).__name__}: {exc}"
        restore_env(old_env)
        provider.close()
    return row


cases = {"N": run_case("N", NEUTRAL_CONSTRAINT), "X": run_case("X", None), "W": run_case("W", WRITE_CONSTRAINT)}


def candidate_view(cand: dict[str, Any]) -> dict[str, Any]:
    content, meta = cand.get("content_json") or {}, cand.get("meta_json") or {}
    return {"title": cand.get("title"), "proposed_scope": cand.get("proposed_scope"), "status": cand.get("status"),
            "source": cand.get("source"), "outcome": content.get("outcome"),
            "args_signature": content.get("args_signature"),
            "source_args_signature": meta.get("source_args_signature"),
            "source_command_id": meta.get("source_command_id")}


def denied_with_args(s: dict[str, Any]) -> bool:
    cands = [candidate_view(x) for x in s.get("new_tool_outcomes", [])]
    return (len(cands) == 1 and cands[0]["title"] == TITLE and cands[0]["proposed_scope"] == f"agent:{AGENT_ID}"
            and cands[0]["status"] == "proposed" and cands[0]["outcome"] == "policy_denied"
            and cands[0]["args_signature"] == CANONICAL_ARGS and cands[0]["source_args_signature"] == CANONICAL_ARGS
            and any("_stage_tool_outcome_candidate" in " ".join(x.get("callers", []))
                    for x in s.get("stage_candidate_calls", [])))


def facts(s: dict[str, Any]) -> dict[str, Any]:
    return {"run_state": s.get("run_state"), "text": s.get("text"), "error": s.get("error"),
            "approval_callback_calls": s.get("approval_callback_calls"),
            "memory_write_decisions": s.get("memory_write_decisions"),
            "new_tool_records": len(s.get("new_tool_records", [])),
            "new_tool_outcomes": [candidate_view(x) for x in s.get("new_tool_outcomes", [])],
            "stagers": [" <- ".join(x.get("callers", [])[:3]) for x in s.get("stage_candidate_calls", [])]}


observed: dict[str, Any] = {"cases": cases, "facts": {}}
for label in ("N", "X"):
    case = cases[label]
    steps = case.get("steps") or {}
    d, a, p = steps.get("D") or {}, steps.get("A") or {}, steps.get("P") or {}
    observed["facts"][label] = {k: facts(v) for k, v in steps.items()}
    name = "NEVER reveal secrets" if label == "N" else "no identity profile"
    turn_decisions = [x for x in d.get("memory_write_decisions", []) if x.get("target") == "memory-capture"]
    c.check(f"{label} ({name}). D: memory.write is allowed for the agent (the turn's memory-capture decision, "
            "where the tree asks one, is allow); the approval callback is asked once for the model's memory.write "
            "call with its arguments and denies it; no `fact:indentation` record is written",
            all(x.get("decision") == "allow" for x in turn_decisions)
            and d.get("approval_callback_calls") == [{"tool": "memory.write", "args": TOOL_ARGS}]
            and not d.get("new_tool_records"),
            facts(d))
    c.check(f"{label}. D: one `tool_outcome` candidate is staged from _stage_tool_outcome_candidate: scope "
            f"agent:{AGENT_ID}, status proposed, title {TITLE}, content.outcome policy_denied, and "
            "content.args_signature and meta.source_args_signature hold the denied call's arguments",
            denied_with_args(d), [candidate_view(x) for x in d.get("new_tool_outcomes", [])])
    c.check(f"{label}. A (control): the callback approves; the `fact:indentation` record is written and no "
            "`tool_outcome` candidate is staged",
            a.get("approval_callback_calls") == [{"tool": "memory.write", "args": TOOL_ARGS}]
            and len(a.get("new_tool_records", [])) == 1 and not a.get("new_tool_outcomes"),
            facts(a))
    batch = case.get("consolidation_batch")
    denied_ids = {x.get("candidate_id") for x in d.get("new_tool_outcomes", [])}
    shown = [x for x in batch if isinstance(x, dict) and x.get("candidate_id") in denied_ids] if isinstance(
        batch, list) else []
    c.check(f"{label}. the memory consolidation context lists the D candidate "
            "(collect_memory_consolidation_candidates, agent scope) with the denied content in its preview",
            len(shown) == 1 and TOOL_ARGS["content"][:15] in str(shown[0].get("content_preview")),
            shown or batch)
observed["facts"]["W"] = {k: facts(v) for k, v in ((cases["W"].get("steps") or {}).items())}
observed["facts"]["P_steps"] = {label: facts((cases[label].get("steps") or {}).get("P") or {})
                                for label in ("N", "X")}
hosts = sorted({h for case in cases.values() for h in case.get("provider_hosts", [])})
errors = {k: case.get("case_error") for k, case in cases.items() if case.get("case_error")}
c.check("Offline provider and no case error: every case's openai base URL is the scripted server on 127.0.0.1",
        hosts == ["127.0.0.1"] and not errors, {"hosts": hosts, "errors": errors})

write_result(OUT, {"info": info, "summary": c.summary(), "all_pass": c.all_pass, "checks": c.items,
                   "observed": observed})
summary = c.summary()
print("W (reference, NEVER write memory records):", json.dumps(observed["facts"]["W"], default=str)[:1500])
print("P (no approval callback):", json.dumps(observed["facts"]["P_steps"], default=str)[:1500])
print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
sys.exit(0 if c.all_pass else 1)
