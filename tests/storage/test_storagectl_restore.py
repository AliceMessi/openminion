"""`storagectl restore` leaves the target equal to the snapshot (#412).

Each test runs the real storagectl commands through `cli.main`: `migrate` and
`backup` a session database, then `restore` onto that live database or onto a
new path. The restored target is compared with the snapshot by its schema,
rows and identity pragmas, not by `ok` or by the file existing.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from openminion.modules.storage import cli as storage_cli
from openminion.modules.storage.migrations import MigrationRunner
from openminion.modules.storage.migrations.backup import BackupError
from openminion.modules.storage.migrations.module_ids import (
    get_module_application_id,
)

TARGETS = pytest.mark.parametrize(
    "target_name",
    ["session.db", "restored.db"],
    ids=["existing-target", "new-target"],
)


class _RecordingReporter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def on_start(self, *, total: int | None, label: str) -> None:
        self.calls.append(("start", label))

    def on_progress(self, *, advance: int = 1, message: str | None = None) -> None:
        self.calls.append(("progress", advance))

    def on_end(self, *, success: bool, message: str | None = None) -> None:
        self.calls.append(("end", success))


def _storagectl(capsys: pytest.CaptureFixture[str], *args: str) -> dict:
    assert storage_cli.main(list(args)) == 0
    return json.loads(capsys.readouterr().out)


def _set_marker(db_path: Path, value: str, *, create: bool = False) -> None:
    with closing(sqlite3.connect(str(db_path))) as conn, conn:
        if create:
            conn.execute("CREATE TABLE probe_marker(v TEXT NOT NULL)")
            conn.execute("INSERT INTO probe_marker(v) VALUES (?)", (value,))
        else:
            conn.execute("UPDATE probe_marker SET v = ?", (value,))


def _content(db_path: Path) -> dict[str, object]:
    with closing(sqlite3.connect(str(db_path))) as conn:
        return {
            "application_id": conn.execute("PRAGMA application_id").fetchone()[0],
            "user_version": conn.execute("PRAGMA user_version").fetchone()[0],
            "tables": sorted(
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            ),
            "dump": list(conn.iterdump()),
        }


def _marker(db_path: Path) -> str:
    with closing(sqlite3.connect(str(db_path))) as conn:
        return conn.execute("SELECT v FROM probe_marker").fetchone()[0]


def _backed_up_session_db(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> tuple[Path, Path]:
    live = tmp_path / "session.db"
    _storagectl(capsys, "migrate", "--namespace", "session", "--sqlite", str(live))
    _set_marker(live, "before-backup", create=True)
    backup = _storagectl(
        capsys,
        "backup",
        "--namespace",
        "session",
        "--sqlite",
        str(live),
        "--snapshot-root",
        str(tmp_path / "snapshots"),
    )
    snapshot = Path(backup["backup"]["snapshot_path"])
    _set_marker(live, "after-backup")
    return live, snapshot


def _record_progress(monkeypatch: pytest.MonkeyPatch) -> _RecordingReporter:
    reporter = _RecordingReporter()
    monkeypatch.setattr(storage_cli, "select_default_reporter", lambda: reporter)
    return reporter


def _restore_args(target: Path, snapshot: Path) -> list[str]:
    return [
        "--progress",
        "restore",
        "--namespace",
        "session",
        "--sqlite",
        str(target),
        "--snapshot-path",
        str(snapshot),
    ]


@TARGETS
def test_storagectl_restore_replaces_the_target_with_the_snapshot(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    target_name: str,
) -> None:
    live, snapshot = _backed_up_session_db(tmp_path, capsys)
    target = tmp_path / target_name
    snapshot_content = _content(snapshot)
    assert "probe_marker" in snapshot_content["tables"]
    assert _marker(snapshot) == "before-backup"
    assert target.exists() == (target == live)
    if target.exists():
        assert _marker(target) == "after-backup"
    reporter = _record_progress(monkeypatch)

    payload = _storagectl(capsys, *_restore_args(target, snapshot))

    assert _content(target) == snapshot_content
    assert _marker(target) == "before-backup"
    assert not list(tmp_path.glob("*.restore.tmp"))
    snapshot_state = MigrationRunner(
        module_id="session",
        db_path=snapshot,
        module_application_id=get_module_application_id("session"),
    ).detect()
    assert list(payload) == ["ok", "state"]
    assert payload["ok"] is True
    assert {**payload["state"], "db_path": None} == {
        **snapshot_state.to_dict(),
        "db_path": None,
    }
    assert payload["state"]["application_id_matches"] is True
    assert Path(payload["state"]["db_path"]).resolve() == target.resolve()
    assert reporter.calls == [("start", "restore[session]"), ("end", True)]


@TARGETS
def test_storagectl_restore_from_a_missing_snapshot_leaves_the_target_alone(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    target_name: str,
) -> None:
    live, _snapshot = _backed_up_session_db(tmp_path, capsys)
    target = tmp_path / target_name
    before = _content(target) if target.exists() else None
    reporter = _record_progress(monkeypatch)

    with pytest.raises(BackupError, match="Snapshot not found"):
        storage_cli.main(_restore_args(target, tmp_path / "missing.sqlite"))

    assert capsys.readouterr().out == ""
    assert reporter.calls == [("start", "restore[session]"), ("end", False)]
    assert not list(tmp_path.glob("*.restore.tmp"))
    if target == live:
        assert _content(target) == before
        assert _marker(target) == "after-backup"
    else:
        assert not target.exists()
