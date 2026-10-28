"""A scheduled memory consolidation turn gets its job's context and skips capture (#456).

Each test builds a real `APIRuntime` (echo provider) and runs cron jobs through
the daemon's own scheduler (`attach_cron_scheduler`): its `CronTurnExecutor`,
turn request builder, runtime manager, ingress, gateway and brain. Jobs are
created through a `TaskManager` on the daemon's cron store, a consolidation job
by the `task.consolidate_memory` handler, and queued with `run_task_now`.

The tests read what each turn leaves behind: its capture intent
(`turn.outcome`) and result (`memory.capture.result`) in the runtime session
store, and its brain working state. They record, without changing them, the
brain's user-message candidate extraction and the consolidation run's capture
recovery.

The built-in memory store applies capture bundles atomically. The bundled
`reference-sqlite` external adapter does not (`CAPTURE_RECOVERY_UNSUPPORTED`),
so an intent it should capture stays pending.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

import openminion.modules.brain.execution as brain_execution
from openminion import Agent, APIRuntime
from openminion.base.config import ConfigManager, OpenMinionConfig, save_config
from openminion.modules.memory.backends.external import (
    REFERENCE_SQLITE_ADAPTER_NAME,
    register_reference_sqlite_backend,
)
from openminion.modules.memory.backends.external import registry
from openminion.modules.memory.errors import StoreWriteError
from openminion.modules.memory.models import MemoryCandidate
from openminion.modules.memory.runtime.consolidation.backend_access import (
    memory_backend,
)
from openminion.modules.task import TaskManager
from openminion.modules.task.constants import DEFAULT_INTEGRATED_SQLITE_SUBPATH
from openminion.modules.tool.runtime import RuntimeContext
from openminion.modules.tool.runtime.policy import Policy
from openminion.services.runtime import daemon
from openminion.tools.task.constants import CONSOLIDATION_PAYLOAD_KEY
from openminion.tools.task.plugin import _h_task_consolidate_memory
from tests._csc_fixtures import _csc_install_default_agent

AGENT_ID = "openminion"
SCOPE = f"agent:{AGENT_ID}"
USER_MESSAGE = "remember that I prefer tabs"
TERMINAL_RUN_STATES = {"finished", "failed", "cancelled", "timed_out"}
# The memory_consolidation_* keys the brain reads, with values a valid job sets.
PUBLIC_CONSOLIDATION_METADATA = {
    "memory_consolidation_job": "true",
    "memory_consolidation_target_scope": SCOPE,
    "memory_consolidation_batch_limit": "5",
    "memory_consolidation_max_iterations": "2",
    "memory_consolidation_timeout_seconds": "30",
}


@pytest.fixture
def reference_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        registry, "_EXTERNAL_BACKENDS", dict(registry._EXTERNAL_BACKENDS)
    )
    register_reference_sqlite_backend()


@pytest.fixture
def extracted(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The user messages the brain extracts memory candidates from."""
    messages: list[str] = []
    real_extract = brain_execution.extract_user_message_candidates

    def recording_extract(runner: Any, *, user_message: str, **kwargs: Any) -> Any:
        messages.append(user_message)
        return real_extract(runner, user_message=user_message, **kwargs)

    monkeypatch.setattr(
        brain_execution, "extract_user_message_candidates", recording_extract
    )
    return messages


def _runtime(
    root: Path, monkeypatch: pytest.MonkeyPatch, *, external: bool = False
) -> Any:
    monkeypatch.setenv("OPENMINION_HOME", str(root / "home"))
    monkeypatch.setenv("OPENMINION_DATA_ROOT", str(root / "home" / "data"))
    config = OpenMinionConfig()
    _csc_install_default_agent(config, provider="echo")
    config.runtime.process_mode = "single-process"
    config.runtime.daemon_auto_start = False
    config.storage.path = str(root / ".openminion" / "state.db")
    save_config(config, str(root / "agents.json"))
    manager = ConfigManager.load(
        str(root / "agents.json"), home_root=root, data_root=root / ".openminion"
    )
    if external:
        memory_config = {
            "store": {"backend": "sqlite", "sqlite_path": str(root / "memory.db")},
            "backend": {
                "provider": "external",
                "external_adapter": REFERENCE_SQLITE_ADAPTER_NAME,
                "options": {"db_path": str(root / "external.sqlite3")},
            },
        }
        manager.register("memory", lambda **_: memory_config)
    return APIRuntime.from_manager(manager)


class _Daemon:
    """The daemon's cron scheduler on a runtime, and a TaskManager on its store."""

    def __init__(self, runtime: Any, root: Path) -> None:
        resolve_db, resolve_brain_db, store_cls, _ = daemon._cron_scheduler_components()
        self.store = daemon._cron_store_for_runtime(
            runtime=runtime,
            resolve_database_path=resolve_db,
            resolve_brain_sessions_db_path=resolve_brain_db,
            sqlite_session_store=store_cls,
        )
        self.tasks = TaskManager.from_cron_repository(
            self.store,
            db_path=(Path(runtime.data_root) / DEFAULT_INTEGRATED_SQLITE_SUBPATH),
        )
        self._root = root
        self.scheduler = daemon.attach_cron_scheduler(
            runtime=runtime, daemon_id="test-456", tick_seconds=0.1
        )
        assert self.scheduler is not None

    def consolidation_job(self, *, batch_limit: int) -> str:
        workspace = self._root / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        policy = Policy(
            raw={
                "workspace_root": str(workspace),
                "context_metadata": {"agent_id": AGENT_ID},
                "paths": {
                    "read_allow": [str(workspace)],
                    "write_allow": [str(workspace)],
                    "deny": [],
                },
                "tools": {"allow_prefix": [""]},
            }
        )
        context = RuntimeContext(
            policy=policy,
            workspace=workspace,
            run_root=self._root / "tool-run",
            scope="WRITE_SAFE",
            confirm=False,
            task_manager=self.tasks,
        )
        created = _h_task_consolidate_memory(
            {"interval_hours": 12, "batch_limit": batch_limit}, context
        )
        return str(created["task_id"])

    def job(self, payload: dict[str, Any]) -> str:
        record = self.tasks.schedule_task(
            name=str(payload["session_id"]),
            schedule={"kind": "every", "every_ms": 12 * 3_600_000},
            payload=payload,
            agent_id=AGENT_ID,
            session_target="isolated",
            delivery={"mode": "none"},
            misfire_policy="skip",
        )
        return str(record.task_id)

    def run(self, task_id: str) -> dict[str, Any]:
        queued = self.tasks.run_task_now(task_id)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            for row in self.store.list_cron_runs(job_id=queued["job_id"], limit=20):
                if row["run_id"] == queued["run_id"] and (
                    row["state"] in TERMINAL_RUN_STATES
                ):
                    return dict(row)
            time.sleep(0.05)
        raise AssertionError(f"cron run {queued['run_id']} did not finish")

    def working_states(self, runtime_session_id: str) -> list[dict[str, Any]]:
        """The brain working state of each turn in a runtime session."""
        states = []
        for session in self.store.list_sessions(limit=100):
            session_id = str(session["session_id"])
            if session_id.startswith(f"{runtime_session_id}::"):
                row = self.store.get_latest_working_state(session_id, agent_id=AGENT_ID)
                states.append(dict(row["state_inline"]))
        return states

    def close(self) -> None:
        self.scheduler.shutdown(grace_s=5)
        self.tasks.close()
        self.store.close()


def _events(runtime: Any, event_type: str, session_id: str | None = None) -> list:
    sessions = runtime.sessions
    return [
        dict(event.payload)
        for session in sessions.list_sessions(limit=100)
        if session_id is None or session.id == session_id
        for event in sessions.list_events(session_id=session.id, limit=1000)
        if event.event_type == event_type
    ]


def _captured_ids(runtime: Any) -> set[str]:
    return {
        str(result["capture_id"])
        for result in _events(runtime, "memory.capture.result")
    }


def _sdk_turn(
    runtime: Any, monkeypatch: pytest.MonkeyPatch, *, fail_capture_write: bool
) -> dict[str, Any]:
    """One SDK turn; returns its `turn.outcome` payload (the capture intent)."""
    with monkeypatch.context() as patch:
        if fail_capture_write:
            memctl = runtime.resolve_memory_assembly(AGENT_ID).memctl

            def failing_write(**_kwargs: Any) -> dict[str, Any]:
                raise StoreWriteError("memory store write failed")

            patch.setattr(memctl, "apply_capture_bundle", failing_write)
        with Agent(runtime=runtime) as agent:
            result = agent.run(USER_MESSAGE, session_id="capture")
    assert result.run_state == "completed"
    [outcome] = _events(runtime, "turn.outcome", "capture")
    return outcome


def _seed_candidates(runtime: Any, candidate_ids: list[str]) -> None:
    runner = runtime.resolve_agent_service(AGENT_ID)._get_runner()
    backend = memory_backend(runner.memory_api)
    for index, candidate_id in enumerate(candidate_ids):
        created_at = f"2026-10-28T00:00:0{index}+00:00"
        backend.candidate_put(
            MemoryCandidate(
                candidate_id=candidate_id,
                session_id="source-session",
                proposed_scope=SCOPE,
                type="fact",
                title=candidate_id,
                content=f"{candidate_id} content",
                source="validated",
                confidence=0.8,
                created_at=created_at,
                updated_at=created_at,
            )
        )


def _record_recoveries(runtime: Any, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    assembly = runtime.resolve_memory_assembly(AGENT_ID)
    recoveries: list[Any] = []
    real_recover = assembly.recover_pending_captures

    def recording_recover(**kwargs: Any) -> Any:
        recoveries.append(real_recover(**kwargs))
        return recoveries[-1]

    monkeypatch.setattr(assembly, "recover_pending_captures", recording_recover)
    return recoveries


def _consolidation_context(state: dict[str, Any]) -> dict[str, Any]:
    context = dict(state["module_state"]["memory_consolidation"])
    context["candidates"] = [item["candidate_id"] for item in context["candidates"]]
    return context


def test_scheduled_consolidation_turn_gets_its_context_and_does_not_capture_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extracted: list[str]
) -> None:
    runtime = _runtime(tmp_path, monkeypatch)
    cron = None
    try:
        intent = _sdk_turn(runtime, monkeypatch, fail_capture_write=True)
        assert intent["capture_state"] == "pending"
        assert _captured_ids(runtime) == set()
        _seed_candidates(runtime, ["cand-older", "cand-newer"])
        recoveries = _record_recoveries(runtime, monkeypatch)
        cron = _Daemon(runtime, tmp_path)
        job_id = cron.consolidation_job(batch_limit=1)
        message = cron.store.get_cron_job(job_id)["payload"]["message"]

        run = cron.run(job_id)
        [outcome] = _events(runtime, "turn.outcome", run["isolated_session_id"])
        [state] = cron.working_states(run["isolated_session_id"])
        captured = _captured_ids(runtime)
    finally:
        if cron is not None:
            cron.close()
        runtime.close()

    assert run["state"] == "finished"
    # The run still recovers the earlier turn's pending intent first.
    assert [(r.scanned, r.recovered, r.pending) for r in recoveries] == [(1, 1, 0)]
    assert intent["capture_id"] in captured
    # The scheduled turn neither extracts from its own message nor captures it.
    assert message not in extracted
    assert outcome["capture_state"] == "excluded"
    assert outcome["capture_id"] not in captured
    # The brain gets the job's consolidation context, within its batch limit.
    assert _consolidation_context(state) == {
        "enabled": True,
        "target_scope": SCOPE,
        "batch_limit": 1,
        "max_iterations": 2,
        "timeout_seconds": 30,
        "candidates": ["cand-older"],
    }
    assert state["resume_cron_job_id_hint"] == job_id


def test_scheduled_consolidation_turns_add_no_pending_intent_without_atomic_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extracted: list[str],
    reference_adapter: None,
) -> None:
    runtime = _runtime(tmp_path, monkeypatch, external=True)
    cron = None
    try:
        intent = _sdk_turn(runtime, monkeypatch, fail_capture_write=False)
        assert intent["capture_state"] == "pending"
        recoveries = _record_recoveries(runtime, monkeypatch)
        cron = _Daemon(runtime, tmp_path)
        job_id = cron.consolidation_job(batch_limit=8)
        message = cron.store.get_cron_job(job_id)["payload"]["message"]

        runs = [cron.run(job_id), cron.run(job_id)]
        [session_id] = {run["isolated_session_id"] for run in runs}
        outcomes = _events(runtime, "turn.outcome", session_id)
        states = cron.working_states(session_id)
        captured = _captured_ids(runtime)
    finally:
        if cron is not None:
            cron.close()
        runtime.close()

    assert [run["state"] for run in runs] == ["finished", "finished"]
    # Each run scans only the earlier turn's intent, which stays pending.
    assert [(r.scanned, r.recovered, r.pending) for r in recoveries] == [
        (1, 0, 1),
        (1, 0, 1),
    ]
    assert captured == set()
    assert message not in extracted
    assert [outcome["capture_state"] for outcome in outcomes] == [
        "excluded",
        "excluded",
    ]
    assert len(states) == 2
    for state in states:
        context = _consolidation_context(state)
        assert (context["enabled"], context["target_scope"]) == (True, SCOPE)
        assert context["batch_limit"] == 8


def test_scheduled_consolidation_keeps_the_scope_and_limit_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extracted: list[str]
) -> None:
    runtime = _runtime(tmp_path, monkeypatch)
    cron = None
    try:
        _seed_candidates(runtime, ["cand-older"])
        cron = _Daemon(runtime, tmp_path)
        # Another agent's scope, and limits the cron path falls back from.
        job_id = cron.job(
            {
                "kind": "agentTurn",
                "message": "Consolidate memory.",
                "session_id": "consolidate:other-scope",
                CONSOLIDATION_PAYLOAD_KEY: {
                    "target_scope": "agent:another-agent",
                    "batch_limit": 0,
                    "max_iterations": 5,
                    "timeout_seconds": "",
                },
            }
        )

        run = cron.run(job_id)
        [outcome] = _events(runtime, "turn.outcome", "consolidate:other-scope")
        [state] = cron.working_states("consolidate:other-scope")
    finally:
        if cron is not None:
            cron.close()
        runtime.close()

    assert run["state"] == "finished"
    assert outcome["capture_state"] == "excluded"
    assert "Consolidate memory." not in extracted
    # The brain still refuses a scope that is not its own agent's, and the
    # cron path's defaults apply to the empty limits.
    assert _consolidation_context(state) == {
        "enabled": True,
        "target_scope": "",
        "batch_limit": 12,
        "max_iterations": 5,
        "timeout_seconds": 30,
        "candidates": [],
    }


def test_user_turns_cannot_assert_a_consolidation_with_public_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extracted: list[str]
) -> None:
    runtime = _runtime(tmp_path, monkeypatch)
    cron = None
    try:
        cron = _Daemon(runtime, tmp_path)
        # The ids of a real consolidation job and run.
        job_id = cron.consolidation_job(batch_limit=8)
        run = cron.run(job_id)
        public = {
            **PUBLIC_CONSOLIDATION_METADATA,
            "cron_job_id": job_id,
            "cron_run_id": run["run_id"],
            "scheduled_for": run["due_at"],
        }
        payloads = {
            "plain": {},
            "meta": {"meta": public},
            "inbound": {"inbound_metadata": public},
        }
        for session_id, extra in payloads.items():
            runtime.submit_turn(
                payload={"message": USER_MESSAGE, "session_id": session_id, **extra}
            ).result(timeout_s=60)
        runtime.run_turn(
            payload={
                "message": USER_MESSAGE,
                "session_id": "direct",
                "inbound_metadata": public,
            }
        )
        sessions = [*payloads, "direct"]
        outcomes = {s: _events(runtime, "turn.outcome", s) for s in sessions}
        states = {s: cron.working_states(s) for s in sessions}
        captured = _captured_ids(runtime)
    finally:
        if cron is not None:
            cron.close()
        runtime.close()

    assert extracted.count(USER_MESSAGE) == 4
    for session_id in sessions:
        [outcome] = outcomes[session_id]
        assert outcome["capture_state"] == "pending", session_id
        assert outcome["capture_id"] in captured, session_id
        [state] = states[session_id]
        assert "memory_consolidation" not in state["module_state"], session_id
    # A cron id still reaches the brain as a resume hint, and asserts nothing.
    assert states["meta"][0]["resume_cron_job_id_hint"] == job_id
    assert states["plain"][0].get("resume_cron_job_id_hint") is None


def test_ordinary_cron_turn_is_captured_with_its_cron_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extracted: list[str]
) -> None:
    runtime = _runtime(tmp_path, monkeypatch)
    cron = None
    try:
        recoveries = _record_recoveries(runtime, monkeypatch)
        cron = _Daemon(runtime, tmp_path)
        jobs = {
            "cron-plain": {},
            "cron-public": {"inbound_metadata": PUBLIC_CONSOLIDATION_METADATA},
        }
        job_ids = {}
        for session_id, extra in jobs.items():
            job_ids[session_id] = cron.job(
                {
                    "kind": "agentTurn",
                    "message": USER_MESSAGE,
                    "session_id": session_id,
                    **extra,
                }
            )
        runs = {s: cron.run(job_id) for s, job_id in job_ids.items()}
        outcomes = {s: _events(runtime, "turn.outcome", s) for s in jobs}
        states = {s: cron.working_states(s) for s in jobs}
        captured = _captured_ids(runtime)
    finally:
        if cron is not None:
            cron.close()
        runtime.close()

    assert recoveries == []
    assert extracted == [USER_MESSAGE, USER_MESSAGE]
    for session_id, job_id in job_ids.items():
        assert runs[session_id]["state"] == "finished", session_id
        [outcome] = outcomes[session_id]
        assert outcome["capture_state"] == "pending", session_id
        assert outcome["capture_id"] in captured, session_id
        [state] = states[session_id]
        assert "memory_consolidation" not in state["module_state"], session_id
        assert state["resume_cron_job_id_hint"] == job_id, session_id
