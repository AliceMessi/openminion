"""`storage_connection` on SQLite runtime storage (#437).

The PostgreSQL side of these checks is in
`tests/integration/test_runtime_storage_postgres.py`.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from openminion.api.core.bootstrap import finalize_runtime_instance
from openminion.base.config import OpenMinionConfig
from openminion.modules.storage.record_store import RecordStoreSQLite
from openminion.modules.storage.runtime.context import RuntimeStorageContext
from openminion.modules.storage.runtime.session_store import SessionStore
from tests._runtime_storage_backend_scenarios import (
    AGENT_ID,
    ROOM_NAME,
    room_create,
    start_runtime,
    start_wrapper,
    state_db,
    write_runtime_config,
)


def test_sqlite_runtime_storage_connection_is_the_record_store_connection(
    tmp_path: Path,
) -> None:
    runtime = start_runtime(write_runtime_config(tmp_path / "root"))
    try:
        record_store = runtime.runtime_storage.record_store
        assert isinstance(record_store, RecordStoreSQLite)
        assert isinstance(runtime.storage_connection, sqlite3.Connection)
        assert runtime.storage_connection is record_store.connection
    finally:
        runtime.close()


def test_sqlite_wrapper_storage_is_the_record_store_connection(tmp_path: Path) -> None:
    wrapper = start_wrapper(write_runtime_config(tmp_path / "root"))
    try:
        runtime = wrapper.runtime_manager
        assert isinstance(wrapper.storage, sqlite3.Connection)
        assert wrapper.storage is runtime.storage_connection
        assert wrapper.storage is runtime.runtime_storage.record_store.connection
        assert isinstance(wrapper.sess, SessionStore)
        assert wrapper.sess is runtime.sessions
    finally:
        wrapper.close()


def test_sqlite_room_create_writes_the_room_to_the_runtime_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "root"
    exit_code, summary = room_create(write_runtime_config(root), monkeypatch, capsys)

    assert exit_code == 0
    assert summary["participants"] == "1"
    assert summary["active_agent"] == AGENT_ID
    connection = sqlite3.connect(state_db(root))
    try:
        assert connection.execute(
            "SELECT json_extract(metadata_json, '$.name') FROM sessions WHERE id = ?",
            (summary["room"],),
        ).fetchall() == [(ROOM_NAME,)]
        assert connection.execute(
            "SELECT participant_type, participant_id, role FROM room_participants"
            " WHERE session_id = ?",
            (summary["room"],),
        ).fetchall() == [("agent", AGENT_ID, "owner")]
    finally:
        connection.close()


def test_sqlite_record_store_without_a_connection_still_fails_the_start(
    tmp_path: Path,
) -> None:
    runtime_storage = RuntimeStorageContext(
        sqlite_path=tmp_path / "state.db",
        migration_result=object(),
        record_store=SimpleNamespace(),
        sessions=object(),
        idempotency=object(),
    )
    paths = SimpleNamespace(
        config=tmp_path / "agents.json",
        home=tmp_path,
        data=tmp_path / ".openminion",
        storage=tmp_path / "state.db",
        memory=tmp_path / "memory",
    )

    class UnexpectedRuntime:
        def __init__(self, **fields: object) -> None:
            raise AssertionError("the runtime was built without a SQLite connection")

    with pytest.raises(
        RuntimeError, match="runtime record_store does not expose sqlite connection"
    ):
        finalize_runtime_instance(
            cls=UnexpectedRuntime,
            base_config=OpenMinionConfig(),
            manager=object(),
            paths=paths,
            infrastructure=defaultdict(object, runtime_storage=runtime_storage),
            agent=object(),
            gateway=object(),
            runtime_mode="direct",
            fallback_reason="",
            effective_run_profile_overrides=object(),
        )
