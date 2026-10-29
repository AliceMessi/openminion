"""Exploration for #494 (not part of the PR): how the stores and the baselines meet."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import traceback
import uuid
from pathlib import Path
from urllib.parse import quote_plus

import sqlalchemy as sa

from openminion.cli.commands.storage import _make_runner
from openminion.modules.storage.backends.postgres import RecordStorePostgres

RESULTS: dict[str, object] = {}
URL = os.environ.get("OPENMINION_TEST_POSTGRES_URL", "")
WORK = Path(tempfile.mkdtemp(prefix="explore494-"))


def say(*parts: object) -> None:
    print(*parts, flush=True)


def case(name):
    def wrap(fn):
        try:
            RESULTS[name] = fn()
        except Exception as exc:  # noqa: BLE001
            RESULTS[name] = {"exception": f"{type(exc).__name__}: {exc}"[:600]}
            traceback.print_exc()
        say("CASE", name, json.dumps(RESULTS[name], default=str)[:1500])
        return fn

    return wrap


def schema_url(name: str) -> str:
    sep = "&" if "?" in URL else "?"
    return f"{URL}{sep}options={quote_plus(f'-csearch_path={name}')}"


def new_schema(prefix: str) -> str:
    name = f"x494_{prefix}_{uuid.uuid4().hex[:8]}"
    eng = sa.create_engine(URL)
    with eng.begin() as conn:
        conn.execute(sa.text(f'CREATE SCHEMA "{name}"'))
    eng.dispose()
    return name


def migrate_pg(module_id: str, schema: str) -> dict:
    engine = sa.create_engine(schema_url(schema))
    try:
        report = _make_runner(module_id, ":memory:", backend_type="postgres", engine=engine).migrate(target="head")
        return {"success": report.success, "error": (report.error or "")[:400], "after": report.after.alembic_revision}
    finally:
        engine.dispose()


def pg_facts(schema: str, tables: tuple[str, ...]) -> dict:
    engine = sa.create_engine(schema_url(schema))
    try:
        with engine.connect() as conn:
            insp = sa.inspect(conn)
            out = {"tables": sorted(insp.get_table_names())}
            for t in tables:
                if insp.has_table(t):
                    out[t] = {
                        c["name"]: [str(c["type"]), c.get("default"), c.get("nullable")]
                        for c in insp.get_columns(t)
                        if c["name"] in ("id", "timestamp", "next_attempt_at")
                    }
            if insp.has_table("alembic_version"):
                out["alembic_version"] = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalars().all()
            if insp.has_table("cp_migrations"):
                out["cp_migrations"] = conn.execute(sa.text("SELECT version FROM cp_migrations ORDER BY version")).scalars().all()
            return out
    finally:
        engine.dispose()


def pg_record_store(schema: str) -> tuple[RecordStorePostgres, object]:
    engine = sa.create_engine(schema_url(schema), future=True)
    return RecordStorePostgres(engine), engine


def cp_round_trip(store) -> dict:
    a = store.put_inbound(chat_key="c", user_key="u", text="hi", payload={"message_id": "m1"})
    b = store.put_inbound(chat_key="c", user_key="u", text="hi2", payload={"message_id": "m2"})
    o = store.put_outbound(chat_key="c", text="out", payload={})
    return {"inbound_ids": [a, b], "outbound_id": o}


def tel_round_trip(store) -> dict:
    from openminion.modules.telemetry.schemas import TelemetryEvent, normalize_telemetry_event

    ev = normalize_telemetry_event(
        TelemetryEvent(session_id="s", turn_id="t", event_type="tool.completed", timestamp=1761650000.123456, data={"k": 1})
    )
    store.insert_event(ev)
    got = store.fetch_session_events("s")
    return {"timestamps": [e.timestamp for e in got], "count": len(got)}


# ---------------- SQLite (unchanged DDL) ----------------
@case("sqlite_cp_migrate_then_store")
def _():
    from openminion.modules.controlplane.storage.migrations import run_migrations
    from openminion.modules.controlplane.storage.store import SQLiteControlPlaneStore

    db = WORK / "cp1.db"
    run_migrations(db)
    store = SQLiteControlPlaneStore(db)
    try:
        return cp_round_trip(store)
    finally:
        store.close()


@case("sqlite_tel_migrate_then_store")
def _():
    from openminion.modules.telemetry.storage.migrations import run_migrations
    from openminion.modules.telemetry.storage.store import SQLiteTelemetryStore

    db = WORK / "tel1.db"
    run_migrations(db)
    store = SQLiteTelemetryStore(db)
    try:
        return tel_round_trip(store)
    finally:
        store.close()


@case("sqlite_cp_store_then_migrate")
def _():
    from openminion.modules.controlplane.storage.migrations import run_migrations
    from openminion.modules.controlplane.storage.store import SQLiteControlPlaneStore

    db = WORK / "cp2.db"
    store = SQLiteControlPlaneStore(db)
    rt = cp_round_trip(store)
    store.close()
    run_migrations(db)
    with sqlite3.connect(db) as conn:
        rev = conn.execute("SELECT version_num FROM alembic_version").fetchall()
    return {"rt": rt, "rev": rev}


# ---------------- PostgreSQL (with the draft fix) ----------------
if URL:

    @case("pg_cp_empty_then_store")
    def _():
        from openminion.modules.controlplane.storage.store import PostgresControlPlaneStore

        s = new_schema("cp_empty")
        m = migrate_pg("controlplane", s)
        facts = pg_facts(s, ("cp_inbound_messages", "cp_outbound_messages", "cp_pair_attempts", "cp_inbox"))
        rs, eng = pg_record_store(s)
        try:
            try:
                store = PostgresControlPlaneStore(record_store=rs)
                rt = cp_round_trip(store)
            except Exception as exc:  # noqa: BLE001
                rt = {"store_exception": f"{type(exc).__name__}: {exc}"[:500]}
        finally:
            eng.dispose()
        return {"migrate": m, "facts": facts, "store": rt, "after": pg_facts(s, ())}

    @case("pg_tel_empty_then_store")
    def _():
        from openminion.modules.telemetry.storage.store import PostgresTelemetryStore

        s = new_schema("tel_empty")
        m = migrate_pg("telemetry", s)
        facts = pg_facts(s, ("events",))
        rs, eng = pg_record_store(s)
        try:
            store = PostgresTelemetryStore(record_store=rs)
            rt = tel_round_trip(store)
        finally:
            eng.dispose()
        return {"migrate": m, "facts": facts, "store": rt}

    @case("pg_cp_store_then_migrate")
    def _():
        from openminion.modules.controlplane.storage.store import PostgresControlPlaneStore

        s = new_schema("cp_store")
        rs, eng = pg_record_store(s)
        store = PostgresControlPlaneStore(record_store=rs)
        rt1 = cp_round_trip(store)
        eng.dispose()
        before = pg_facts(s, ("cp_inbound_messages", "cp_outbound_messages", "cp_pair_attempts", "cp_inbox"))
        m = migrate_pg("controlplane", s)
        m2 = migrate_pg("controlplane", s)
        rs, eng = pg_record_store(s)
        store = PostgresControlPlaneStore(record_store=rs)
        rt2 = cp_round_trip(store)
        eng.dispose()
        return {"rt1": rt1, "before": before, "migrate": m, "rerun": m2, "rt2": rt2, "after": pg_facts(s, ("cp_inbound_messages",))}

    @case("pg_tel_store_then_migrate")
    def _():
        from openminion.modules.telemetry.storage.store import PostgresTelemetryStore

        s = new_schema("tel_store")
        rs, eng = pg_record_store(s)
        store = PostgresTelemetryStore(record_store=rs)
        rt1 = tel_round_trip(store)
        eng.dispose()
        m = migrate_pg("telemetry", s)
        rs, eng = pg_record_store(s)
        store = PostgresTelemetryStore(record_store=rs)
        rt2 = tel_round_trip(store)
        eng.dispose()
        return {"rt1": rt1, "migrate": m, "rt2": rt2, "after": pg_facts(s, ("events",))}

    def _precreate(prefix: str, ddl: str, module_id: str, tables: tuple[str, ...]) -> dict:
        s = new_schema(prefix)
        eng = sa.create_engine(schema_url(s))
        with eng.begin() as conn:
            conn.exec_driver_sql(ddl)
        eng.dispose()
        m = migrate_pg(module_id, s)
        return {"migrate": m, "after": pg_facts(s, tables)}

    @case("pg_tel_precreated_real_timestamp")
    def _():
        return _precreate(
            "tel_real",
            "CREATE TABLE events (id SERIAL PRIMARY KEY, session_id TEXT NOT NULL, turn_id TEXT NOT NULL,"
            " event_type TEXT NOT NULL, timestamp REAL NOT NULL, data TEXT NOT NULL)",
            "telemetry",
            ("events",),
        )

    @case("pg_tel_precreated_session_events")
    def _():
        return _precreate(
            "tel_sess",
            "CREATE TABLE events (event_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, ts TEXT NOT NULL,"
            " type TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}')",
            "telemetry",
            ("events",),
        )

    @case("pg_cp_precreated_plain_integer_id")
    def _():
        return _precreate(
            "cp_int",
            "CREATE TABLE cp_inbound_messages (id INTEGER PRIMARY KEY, message_id TEXT NOT NULL, chat_key TEXT NOT NULL,"
            " user_key TEXT NOT NULL, session_id TEXT, agent_id TEXT, timestamp TEXT NOT NULL, text TEXT,"
            " payload_json TEXT NOT NULL)",
            "controlplane",
            ("cp_inbound_messages",),
        )

    @case("pg_downgrade_upgrade")
    def _():
        from alembic import command
        from alembic.config import Config
        from openminion.modules.storage.migrations.alembic import discover_module_alembic_paths

        out = {}
        for module_id in ("controlplane", "telemetry"):
            s = new_schema(f"down_{module_id}")
            m = migrate_pg(module_id, s)
            ini, loc = discover_module_alembic_paths(module_id)
            cfg = Config(str(ini))
            cfg.set_main_option("script_location", str(loc))
            cfg.set_main_option("sqlalchemy.url", schema_url(s).replace("%", "%%"))
            command.downgrade(cfg, "base")
            mid = pg_facts(s, ())
            m2 = migrate_pg(module_id, s)
            out[module_id] = {"migrate": m, "after_downgrade": mid, "remigrate": m2, "final": pg_facts(s, ())}
        return out


(WORK / "results.json").write_text(json.dumps(RESULTS, indent=2, default=str), encoding="utf-8")
out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
out_dir.mkdir(parents=True, exist_ok=True)
(out_dir / "explore_494.json").write_text(json.dumps(RESULTS, indent=2, default=str), encoding="utf-8")
say("done")
