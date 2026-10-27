from __future__ import annotations

import gc
import os
import sqlite3
from pathlib import Path

import pytest

from openminion import Agent, APIRuntime
from openminion.base.config import OpenMinionConfig, save_config
from openminion.modules.storage.migrations import MigrationRunner
from openminion.modules.storage.migrations.module_ids import get_module_application_id
from tests._csc_fixtures import _csc_install_default_agent

MODULE_APP_ID = 0x4F4D0001
SECRET_APP_ID = get_module_application_id("secret")


def _init_db(path: Path) -> None:
    with sqlite3.connect(str(path)) as conn:
        conn.execute(f"PRAGMA application_id = {MODULE_APP_ID}")
        conn.execute("PRAGMA user_version = 1")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS alembic_version(version_num TEXT PRIMARY KEY)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO alembic_version(version_num) VALUES ('0001_init')"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS om_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO om_meta(key, value) VALUES ('module_id', 'sessctl')"
        )
    conn.close()


def _track_connections(monkeypatch: pytest.MonkeyPatch) -> list[sqlite3.Connection]:
    # Strong references: an unclosed connection stays open (not collected) until asserted.
    opened: list[sqlite3.Connection] = []
    connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        conn = connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)
    return opened


def _is_open(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return False
    return True


def _om_meta(path: Path) -> dict[str, str]:
    conn = sqlite3.connect(str(path))
    try:
        return dict(conn.execute("SELECT key, value FROM om_meta").fetchall())
    finally:
        conn.close()


def _module_runner(tmp_path: Path) -> tuple[MigrationRunner, Path]:
    db_path = tmp_path / "module.db"
    _init_db(db_path)
    runner = MigrationRunner(
        module_id="sessctl",
        db_path=db_path,
        module_application_id=MODULE_APP_ID,
        snapshot_root=tmp_path / "snapshots",
    )
    return runner, db_path


def test_migrate_closes_every_sqlite_connection_it_opens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "secret.db"
    db_path.touch()
    runner = MigrationRunner(
        module_id="secret",
        db_path=db_path,
        module_application_id=SECRET_APP_ID,
    )
    opened = _track_connections(monkeypatch)

    report = runner.migrate(target="head")

    assert report.success is True
    assert opened
    assert [_is_open(conn) for conn in opened] == [False] * len(opened)


def test_detect_and_verify_close_their_connections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _ = _module_runner(tmp_path)
    opened = _track_connections(monkeypatch)

    state = runner.detect()
    detect_opened = list(opened)
    verification = runner.verify(level="quick")

    assert state.application_id == MODULE_APP_ID
    assert verification.ok is True
    assert len(detect_opened) == 1
    assert len(opened) == 2
    assert [_is_open(conn) for conn in opened] == [False, False]


def test_metadata_update_commits_and_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, db_path = _module_runner(tmp_path)
    opened = _track_connections(monkeypatch)

    runner._update_sqlite_metadata_after_migration(
        target="head", migrated_at="2026-10-24T00:00:00+00:00"
    )

    assert len(opened) == 1
    assert _is_open(opened[0]) is False
    meta = _om_meta(db_path)
    assert meta["schema_head"] == "0001_init"
    assert meta["last_migrated_at"] == "2026-10-24T00:00:00+00:00"


def test_metadata_update_rolls_back_and_closes_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, db_path = _module_runner(tmp_path)
    opened = _track_connections(monkeypatch)

    def failing_upsert(
        *, connection: sqlite3.Connection, values: dict[str, str]
    ) -> None:
        del values
        connection.execute(
            "INSERT INTO om_meta(key, value) VALUES ('probe', 'written')"
        )
        raise RuntimeError("forced metadata failure")

    monkeypatch.setattr(runner, "_upsert_sqlite_om_meta", failing_upsert)

    with pytest.raises(RuntimeError, match="forced metadata failure"):
        runner._update_sqlite_metadata_after_migration(
            target="head", migrated_at="2026-10-24T00:00:00+00:00"
        )

    assert len(opened) == 1
    assert _is_open(opened[0]) is False
    assert "probe" not in _om_meta(db_path)


def test_database_file_can_be_renamed_after_migrate_without_gc(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "secret.db"
    db_path.touch()
    runner = MigrationRunner(
        module_id="secret",
        db_path=db_path,
        module_application_id=SECRET_APP_ID,
    )
    moved = tmp_path / "secret.moved.db"

    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        assert runner.migrate(target="head").success is True
        # On Windows an open handle makes this raise PermissionError (WinError 32).
        os.replace(db_path, moved)
    finally:
        if gc_was_enabled:
            gc.enable()

    assert moved.is_file()


def test_runtime_store_file_can_be_renamed_after_close_without_gc(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    config = OpenMinionConfig()
    _csc_install_default_agent(config, provider="echo")
    config.runtime.process_mode = "single-process"
    config.runtime.daemon_auto_start = False
    config.storage.path = str(root / ".openminion" / "state.db")
    config_path = root / "agents.json"
    save_config(config, str(config_path))
    store = root / ".openminion" / "authored_tools" / "store.sqlite"

    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        runtime = APIRuntime.from_config_path(
            str(config_path), home_root=root, data_root=root / ".openminion"
        )
        try:
            with Agent(runtime=runtime) as agent:
                assert agent.run("hello").run_state == "completed"
        finally:
            runtime.close()
        assert store.is_file()
        os.replace(store, store.with_name("store.moved.sqlite"))
    finally:
        if gc_was_enabled:
            gc.enable()

    assert store.with_name("store.moved.sqlite").is_file()
