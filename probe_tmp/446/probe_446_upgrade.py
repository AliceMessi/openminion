"""#446: installed upgrade and restore, from published releases to the branch build, on PostgreSQL.

    <branch venv>/bin/python -I -X utf8 probe_446_upgrade.py drive RELEASE <release venv python> OUT.json

For every owner that the published RELEASE can build on PostgreSQL, on a new database each:
1. build: the RELEASE (its own venv, installed from PyPI) builds the owner's layout in `public`, the
   way an installation of that release does (runtime storage start, `MigrationRunner.migrate("head")`
   as `storage migrate` runs it, or the module store), and the probe adds one row to every table it
   can (generic values for NOT NULL columns without a default);
2. the layout is read (`snapshot`), and the database is dumped with `pg_dump`;
3. upgrade: the branch build (installed from the checkout into its own venv) opens the same owner
   through its production entry point: `build_runtime_storage`, the installed `openminion storage
   migrate --backend postgres` console script, `resolve_memory_backend`, `build_a2a_audit_store`, or
   the module store factory;
4. checks: the branch accepted the layout in place (no `<base>__<module>` schema was created), the
   owner's layout afterwards is one of the branch's listed histories built by the current code
   (`postgres_histories.json`, measured "current"), and every row that existed before is unchanged
   (row count and md5 over the columns it had before);
5. restore: the pre-upgrade dump is restored into a new database, and the RELEASE opens it again
   (rollback means restoring the pre-upgrade dump with the previous package); its layout must equal
   the pre-upgrade one.

One mixed case, `runtime+session_store`, builds runtime storage and then the session store on the
same URL (the session trace query of the API does that), whether or not the store succeeds there;
the branch must start runtime storage in place, and `storage migrate --module session` and the
session store then create `public__session` only.

Modes `build`, `upgrade`, `reopen` run one step for every case in a plan file; `drive` runs them all.
Evidence for #446, not the implementation.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import text

BASE = os.environ["OPENMINION_TEST_POSTGRES_URL"]
SYSTEM = "n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'"
ENGINE_TABLES = {"core_events", "core_sidecar_rows", "sidecar_ingest_log"}
LEDGER_TABLES = {"alembic_version", "om_meta"}
os.environ.pop("PGOPTIONS", None)
os.environ.setdefault("OPENMINION_SECRET_KEY", "ZmFrZS1rZXktZm9yLXRoZS00NDYtcHJvYmUtb25seS0=")

MODULE_STORES = {
    # case: (module id, factory module, factory name, extra kwargs)
    "registry_store": ("registry", "openminion.modules.registry.storage", "build_registry_store", "registry.db"),
    "artifact_index": ("artifact", "openminion.modules.artifact.storage", "build_artifact_index", "artifact.db"),
    "identity_store": ("identity", "openminion.modules.identity.storage", "build_identity_store", "identity.db"),
    "policy_store": ("policy", "openminion.modules.policy.storage", "build_policy_store", "policy.db"),
    "retrieve_store": ("retrieve", "openminion.modules.retrieve.storage", "build_retrieve_store", "retrieve.db"),
    "skill_store": ("skill", "openminion.modules.skill.storage", "build_skill_store", "skill.db"),
    "task_store": ("task", "openminion.modules.task.storage", "build_task_store", "task.db"),
    "telemetry_store": ("telemetry", "openminion.modules.telemetry.storage", "build_telemetry_store", "telemetry.db"),
    "compress_store": ("compress", "openminion.modules.context.compress.storage", "build_compress_telemetry_store",
                       "compress.db"),
    "controlplane_store": ("controlplane", "openminion.modules.controlplane.storage", "build_controlplane_store",
                           "cp.db"),
    "a2a_state_store": ("a2a", "openminion.modules.a2a.storage.factory", "build_a2a_state_store", "a2a.db"),
    "session_store": ("session", "openminion.modules.session.runtime.factory", "build_module_session_store",
                      "sessions.db"),
}
ALEMBIC_MODULES = ["memory", "a2a", "session", "registry", "artifact", "identity", "policy", "retrieve",
                   "secret", "skill", "task", "compress", "controlplane", "telemetry"]


def short_exc(exc: BaseException) -> str:
    frames = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    where = f" (at {Path(frames[-1].filename).name}:{frames[-1].lineno})" if frames else ""
    first = str(exc).splitlines()[0][:20000] if str(exc) else ""
    return f"{type(exc).__name__}: {first}{where}"


def qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def cases() -> list[str]:
    after = [f"after:{name}" for name, spec in MODULE_STORES.items() if spec[0] in ALEMBIC_MODULES]
    before = [f"before:{name}" for name, spec in MODULE_STORES.items() if spec[0] in ALEMBIC_MODULES]
    return (["runtime", "memory_store", "a2a_audit_store"] + [f"alembic:{m}" for m in ALEMBIC_MODULES]
            + list(MODULE_STORES) + after + before + [MIXED])


# Runtime storage, then the session store on the same URL, as the API's session trace query
# builds it; the store may fail there and leave objects next to runtime storage. The branch
# starts runtime storage, runs `storage migrate --module session`, then opens the session store.
MIXED = "runtime+session_store"


# ---------------------------------------------------------------------------- owner steps

def storage_config(url: str, root: Path):
    from openminion.modules.storage.engine import StorageEngineConfig

    root.mkdir(parents=True, exist_ok=True)
    return StorageEngineConfig(root_dir=root, sqlite_path=root / "state.db", fallback_root=root,
                               record_backend="record.postgres", record_backend_options={"url": url})


def close_quietly(obj) -> None:
    for name in ("close",):
        fn = getattr(obj, name, None)
        if callable(fn):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass


def open_owner(case: str, url: str, root: Path, *, branch: bool) -> str:
    """Open (build) the owner the way an installation does; return a short note."""
    root.mkdir(parents=True, exist_ok=True)
    if case == MIXED:
        notes = [open_owner("runtime", url, root / "runtime", branch=branch)]
        steps = ["alembic:session", "session_store"] if branch else ["session_store"]
        for step in steps:
            try:
                notes.append(open_owner(step, url, root / step.replace(":", "_"), branch=branch))
            except Exception as exc:  # noqa: BLE001
                if branch:
                    raise
                notes.append(f"session store failed: {short_exc(exc)[:300]}")
        return " | ".join(notes)
    if case.startswith(("after:", "before:")):
        # The release built `storage migrate --module <m>` and the module store on one database,
        # in the case's order ("after": the store after the migration). The branch build is
        # upgraded as the docs say: `storage migrate` first, then the store.
        name = case.split(":", 1)[1]
        alembic = f"alembic:{MODULE_STORES[name][0]}"
        order = [alembic, name] if branch or case.startswith("after:") else [name, alembic]
        return " | ".join(open_owner(step, url, root / step.replace(":", "_"), branch=branch)
                          for step in order)
    if case == "runtime":
        from openminion.modules.storage.runtime.context import build_runtime_storage

        ctx = build_runtime_storage(root / "state.db", env={}, record_backend="record.postgres",
                                    record_backend_options={"url": url})
        try:
            return f"runtime version {ctx.migration_result.current_version}"
        finally:
            ctx.close()
    if case == "memory_store":
        if branch:
            from openminion.modules.memory.storage.factory import resolve_memory_backend

            resolved = resolve_memory_backend(
                config={"store": {"backend": "postgres", "postgres_url": url}}, db_path=root / "memory.db")
            close_quietly(resolved.store)
            return f"resolve_memory_backend -> {type(resolved.store).__name__}"
        from openminion.modules.memory.storage.postgres.store import PostgresMemoryStore

        store = PostgresMemoryStore(sa.create_engine(url, future=True), database_path=root / "memory.db",
                                    owns_engine=True)
        close_quietly(store)
        return "PostgresMemoryStore"
    if case == "a2a_audit_store":
        from openminion.modules.a2a.storage.factory import build_a2a_audit_store

        store = build_a2a_audit_store(config=storage_config(url, root), audit_root=root / "a2a-audit")
        close_quietly(store)
        return type(store).__name__
    if case.startswith("alembic:"):
        module_id = case.split(":", 1)[1]
        if branch:
            exe = Path(sys.executable).with_name("openminion")
            proc = subprocess.run(
                [str(exe), "storage", "migrate", "--backend", "postgres", "--postgres-url", url, "--module",
                 module_id, "--json"], capture_output=True, text=True, cwd=root, timeout=600)
            payload = proc.stdout.strip()
            if proc.returncode != 0:
                try:
                    errors = [" ".join(str(m.get("error")).split()) for m in json.loads(payload)["modules"]
                              if m.get("error")]
                except ValueError:
                    errors = [" ".join(payload.split())[:3000]]
                stderr = " ".join(proc.stderr.split())[-300:]
                raise RuntimeError(f"storage migrate exit {proc.returncode}: {' | '.join(errors)} {stderr}")
            return f"storage migrate --json: {payload[:300]}"
        from openminion.modules.storage.migrations.module_ids import get_module_application_id
        from openminion.modules.storage.migrations.runner import MigrationRunner

        eng = sa.create_engine(url)
        try:
            report = MigrationRunner(module_id=module_id, db_path=root / f"{module_id}.db",
                                     module_application_id=get_module_application_id(module_id),
                                     backend_type="postgres", engine=eng).migrate(target="head")
        finally:
            eng.dispose()
        if not report.success:
            raise RuntimeError(str(report.error).splitlines()[0][:300] if report.error else "failed")
        return f"MigrationRunner head: {report.after.alembic_revision}"
    module_id, module, name, db_name = MODULE_STORES[case]
    import importlib

    factory = getattr(importlib.import_module(module), name)
    kwargs = {"config": storage_config(url, root), "database_path": root / db_name}
    if case == "session_store":
        kwargs["env"] = {}
    store = factory(**kwargs)
    close_quietly(store)
    return type(store).__name__


VALUES = {"text": "'x'", "character varying": "'x'", "integer": "1", "bigint": "1", "smallint": "1",
          "double precision": "1.0", "real": "1.0", "numeric": "1", "boolean": "false", "jsonb": "'{}'::jsonb",
          "json": "'{}'::json", "bytea": "'\\x00'::bytea", "timestamp with time zone": "now()",
          "timestamp without time zone": "now()", "date": "current_date"}


def add_rows(url: str) -> list[str]:
    """One row in every non-ledger table, with generic values for NOT NULL columns without default."""
    eng = sa.create_engine(url)
    added = []
    with eng.connect() as conn:
        tables = [r[0] for r in conn.execute(text(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY 1"))]
        for table in tables:
            if table in LEDGER_TABLES or table in ENGINE_TABLES or table == "migrations":
                continue
            cols = conn.execute(text(
                "SELECT a.attname, format_type(a.atttypid, NULL) FROM pg_attribute a "
                "JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname = :t AND a.attnum > 0 AND NOT a.attisdropped "
                "AND a.attnotnull AND a.atthasdef = false AND a.attidentity = '' AND a.attgenerated = ''"),
                {"t": table}).all()
            if any(typ not in VALUES for _, typ in cols):
                continue
            names = ", ".join(qi(c) for c, _ in cols)
            values = ", ".join(VALUES[t] for _, t in cols)
            sql = (f"INSERT INTO public.{qi(table)} ({names}) VALUES ({values})" if cols
                   else f"INSERT INTO public.{qi(table)} DEFAULT VALUES")
            try:
                with conn.begin_nested() if conn.in_transaction() else conn.begin():
                    conn.execute(text(sql))
                added.append(table)
            except Exception:  # noqa: BLE001
                pass
        if conn.in_transaction():
            conn.commit()
    eng.dispose()
    return added


def snapshot(url: str) -> dict:
    eng = sa.create_engine(url)
    out: dict = {}
    with eng.connect() as conn:
        for (schema,) in conn.execute(text(f"SELECT n.nspname FROM pg_namespace n WHERE {SYSTEM} ORDER BY 1")):
            oid = conn.execute(text("SELECT oid FROM pg_namespace WHERE nspname = :s"), {"s": schema}).scalar()
            cols: dict = {}
            for rel, att, typ, notnull in conn.execute(text(
                    "SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull "
                    "FROM pg_class c JOIN pg_attribute a ON a.attrelid = c.oid WHERE c.relnamespace = :o "
                    "AND c.relkind IN ('r','p') AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum"),
                    {"o": oid}):
                cols.setdefault(rel, []).append([att, typ, bool(notnull)])
            pks: dict = {}
            for rel, att in conn.execute(text(
                    "SELECT c.relname, a.attname FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid "
                    "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey) "
                    "WHERE i.indisprimary AND c.relnamespace = :o"), {"o": oid}):
                pks.setdefault(rel, []).append(att)
            tables = {}
            for rel, columns in sorted(cols.items()):
                names = ", ".join(qi(c[0]) for c in columns)
                count, md5 = conn.execute(text(
                    f"SELECT count(*), md5(coalesce(string_agg(r::text, '|' ORDER BY r::text), '')) "
                    f"FROM (SELECT ROW({names}) AS r FROM {qi(schema)}.{qi(rel)}) s")).one()
                tables[rel] = {"columns": sorted(columns), "pk": sorted(pks.get(rel, [])), "rows": int(count),
                               "md5": md5, "column_order": [c[0] for c in columns]}
                if count <= 5000:
                    tables[rel]["row_texts"] = sorted(str(r[0]) for r in conn.execute(text(
                        f"SELECT ROW({names})::text FROM {qi(schema)}.{qi(rel)}")))
            ledgers: dict = {}
            if "alembic_version" in cols:
                ledgers["alembic_version"] = sorted(str(r[0]) for r in conn.execute(text(
                    f"SELECT version_num FROM {qi(schema)}.alembic_version")))
            if "om_meta" in cols:
                ledgers["om_meta"] = {str(k): str(v) for k, v in conn.execute(text(
                    f"SELECT key, value FROM {qi(schema)}.om_meta")) if k in ("module_id", "schema_head")}
            if "migrations" in cols and any(c[0] == "name" for c in cols["migrations"]):
                ledgers["migrations"] = [[int(v), str(n)] for v, n in conn.execute(text(
                    f"SELECT version, name FROM {qi(schema)}.migrations ORDER BY version"))]
            if tables or schema != "public":
                out[schema] = {"tables": tables, "ledgers": ledgers}
    eng.dispose()
    return out


def row_texts(url: str, schema: str, table: str, columns: list[str]) -> list[str]:
    """Every row of the table over the given columns, as ROW(...)::text."""
    eng = sa.create_engine(url)
    names = ", ".join(qi(c) for c in columns)
    with eng.connect() as conn:
        rows = sorted(str(r[0]) for r in conn.execute(text(
            f"SELECT ROW({names})::text FROM {qi(schema)}.{qi(table)}")))
    eng.dispose()
    return rows


# ---------------------------------------------------------------------------- per-plan steps

def run_step(mode: str, plan_path: Path) -> int:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    import openminion

    results = {"version": getattr(openminion, "__version__", "?"), "cases": {}}
    for case in plan["cases"]:
        url = plan["urls"][case] if mode != "reopen" else plan["restored"][case]
        root = Path(plan["root"]) / mode / case.replace(":", "_")
        started = time.monotonic()
        try:
            note = open_owner(case, url, root, branch=(mode == "upgrade"))
            entry = {"ok": True, "note": note}
            if mode == "build":
                entry["rows_added"] = add_rows(url)
        except Exception as exc:  # noqa: BLE001
            entry = {"ok": False, "error": short_exc(exc)}
        entry["seconds"] = round(time.monotonic() - started, 2)
        results["cases"][case] = entry
        print(f"{mode.upper()} {case} | {json.dumps(entry)[:500]}", flush=True)
    Path(plan["root"], f"{mode}.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    return 0


def admin():
    return sa.create_engine(BASE, isolation_level="AUTOCOMMIT")


def db_url(name: str) -> str:
    return sa.engine.make_url(BASE).set(database=name).render_as_string(hide_password=False)


def create_db(name: str) -> None:
    eng = admin()
    with eng.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {qi(name)}"))
        conn.execute(text(f"CREATE DATABASE {qi(name)}"))
    eng.dispose()


def drop_db(name: str) -> None:
    eng = admin()
    with eng.connect() as conn:
        conn.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :d "
                          "AND pid <> pg_backend_pid()"), {"d": name})
        conn.execute(text(f"DROP DATABASE IF EXISTS {qi(name)}"))
    eng.dispose()


def docker(*args: str, stdin=None, stdout=None) -> None:
    subprocess.run(["docker", "exec", "-i", "pg", *args], check=True, stdin=stdin, stdout=stdout,
                   stderr=subprocess.PIPE)


def current_histories() -> list[dict]:
    from openminion.modules.storage.migrations import postgres_schema

    data = json.loads(Path(postgres_schema.__file__).with_name("postgres_histories.json").read_text(encoding="utf-8"))
    sigs = data["signatures"]
    out = []
    for h in data["histories"]:
        out.append({"owner": h["owner"], "ledger": h["ledger"], "current": "current" in h["measured"],
                    "tables": {ref.split("/")[0]: sigs[ref] for ref in h["tables"]}})
    return out


def sig_of(table: dict) -> str:
    cols = ", ".join(f"{n} {t}{' NOT NULL' if nn else ''}" for n, t, nn in sorted(map(tuple, table["columns"])))
    return f"{cols}; pk({', '.join(sorted(table['pk']))})"


def owner_of(case: str) -> str:
    if case in ("runtime", MIXED):
        return "runtime"
    if case == "memory_store":
        return "memory"
    if case == "a2a_audit_store":
        return "a2a"
    if case.startswith("alembic:"):
        return case.split(":", 1)[1]
    return MODULE_STORES[case.split(":", 1)[-1]][0]


def ledger_of(owner: str, block: dict) -> str | None:
    ledgers = block.get("ledgers", {})
    if owner == "runtime":
        rows = ledgers.get("migrations") or []
        return f"v{len(rows)}" if rows else None
    meta = ledgers.get("om_meta") or {}
    if meta.get("module_id") != owner:
        return None
    alembic = ledgers.get("alembic_version")
    if alembic is None:
        return f"store {meta.get('schema_head', '')}"
    return f"alembic {alembic[0]}" if len(alembic) == 1 else None


def listed_match(owner: str, block: dict, histories: list[dict], *, current_only: bool) -> str | None:
    ledger = ledger_of(owner, block)
    tables = {t: sig_of(spec) for t, spec in block["tables"].items()
              if t not in ENGINE_TABLES and t not in LEDGER_TABLES}
    names = {t for h in histories if h["owner"] == owner for t in h["tables"]}
    owned = {t: s for t, s in tables.items() if t in names}
    for h in histories:
        if h["owner"] == owner and h["ledger"] == ledger and h["tables"] == owned:
            if current_only and not h["current"]:
                continue
            return f"{owner} {ledger}" + ("" if h["current"] else " (historical)")
    return None


def drive(release: str, release_python: str, out: Path) -> int:
    token = f"u{release.replace('.', '')}_{uuid.uuid4().hex[:4]}"
    root = Path(tempfile.mkdtemp(prefix=f"p446u-{release}-"))
    names = {case: f"{token}_{i}" for i, case in enumerate(cases())}
    restored = {case: f"{name}_r" for case, name in names.items()}
    for name in names.values():
        create_db(name)
    plan = {"cases": cases(), "urls": {c: db_url(n) for c, n in names.items()},
            "restored": {c: db_url(n) for c, n in restored.items()}, "root": str(root)}
    plan_path = root / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    me = str(Path(__file__).resolve())
    empty = root / "cwd"
    empty.mkdir()

    def step(python: str, mode: str) -> dict:
        proc = subprocess.run([python, "-I", "-X", "utf8", me, mode, str(plan_path)], cwd=empty,
                              capture_output=True, text=True, timeout=3600)
        sys.stdout.write(proc.stdout)
        if proc.returncode != 0:
            print(f"STEP {mode} exit {proc.returncode}: {proc.stderr[-800:]}", flush=True)
        path = root / f"{mode}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"cases": {}}

    report: dict = {"release": release, "cases": {}}
    built = step(release_python, "build")
    report["release_version"] = built.get("version")
    before = {}
    for case, name in names.items():
        if built["cases"].get(case, {}).get("ok"):
            before[case] = snapshot(plan["urls"][case])
            with open(root / f"{name}.sql", "wb") as fh:
                docker("pg_dump", "-U", "postgres", "--no-owner", "-d", name, stdout=fh)
    histories = current_histories()
    upgraded = step(sys.executable, "upgrade")
    report["branch_version"] = upgraded.get("version")
    for case, name in names.items():
        entry: dict = {"build": built["cases"].get(case)}
        report["cases"][case] = entry
        if case not in before:
            entry["verdict"] = "not built by this release"
            continue
        owner = owner_of(case)
        pre = before[case]
        entry["before_tables"] = {s: sorted(b["tables"]) for s, b in pre.items()}
        entry["before_listed"] = listed_match(owner, pre.get("public", {"tables": {}}), histories,
                                              current_only=False)
        entry["upgrade"] = upgraded["cases"].get(case)
        after = snapshot(plan["urls"][case])
        entry["unchanged_by_refusal"] = after == pre
        entry["after_tables"] = {s: sorted(b["tables"]) for s, b in after.items()}
        new_schemas = sorted(set(after) - set(pre))
        entry["new_schemas"] = new_schemas
        entry["after_listed_current"] = listed_match(owner, after.get("public", {"tables": {}}), histories,
                                                     current_only=True)
        # Every row that existed before is still there, over the columns it had (rows the
        # upgrade appends, such as a store's own migration ledger rows, are reported apart).
        changed, appended = [], []
        for t, spec in pre.get("public", {}).get("tables", {}).items():
            if t in LEDGER_TABLES:
                continue
            if t not in after.get("public", {}).get("tables", {}):
                changed.append(f"{t} gone")
                continue
            now = row_texts(plan["urls"][case], "public", t, spec["column_order"])
            lost = len(set(spec.get("row_texts", [])) - set(now))
            if lost or len(now) < spec["rows"]:
                changed.append(f"{t} lost {lost} of {spec['rows']} rows")
            elif len(now) > spec["rows"]:
                appended.append(f"{t} +{len(now) - spec['rows']}")
        entry["rows_changed"] = changed
        entry["rows_appended"] = appended
        entry["before_layout"] = {t: sig_of(v) for t, v in pre.get("public", {}).get("tables", {}).items()
                                  if t not in ENGINE_TABLES and t not in LEDGER_TABLES}
        entry["ledgers_before"] = pre.get("public", {}).get("ledgers")
        entry["ledgers_after"] = after.get("public", {}).get("ledgers")
        create_db(restored[case])
        with open(root / f"{name}.sql", "rb") as fh:
            docker("psql", "-q", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", restored[case], stdin=fh)
        entry["restored_equal_before"] = None
    reopened = step(release_python, "reopen")
    for case in names:
        entry = report["cases"][case]
        if case not in before:
            continue
        entry["reopen"] = reopened["cases"].get(case)
        restored_snap = snapshot(plan["restored"][case])

        def shape(snap: dict) -> dict:
            # om_meta's last_migrated_at changes on every Alembic run; its ledger keys are compared.
            return {s: ({t: (v["columns"], v["pk"]) + (() if t == "om_meta" else (v["rows"], v["md5"]))
                         for t, v in b["tables"].items()}, b["ledgers"]) for s, b in snap.items()}

        entry["restored_equal_before"] = shape(restored_snap) == shape(before[case])
        restored_ok = bool(entry["reopen"] and entry["reopen"].get("ok")) and entry["restored_equal_before"]
        upgraded = bool(entry["upgrade"] and entry["upgrade"].get("ok"))
        expected_schemas = ["public__session"] if case == MIXED else []
        ok = (upgraded and entry["new_schemas"] == expected_schemas and bool(entry["after_listed_current"])
              and not entry["rows_changed"] and restored_ok)
        refused = (not upgraded and "Nothing was written" in str(entry["upgrade"])
                   and entry["unchanged_by_refusal"] and restored_ok)
        entry["verdict"] = "PASS" if ok else ("REFUSED" if refused else "FAIL")
        print(f"CASE {release} {case} | {entry['verdict']} | before={entry['before_listed']} "
              f"after={entry['after_listed_current']} new_schemas={entry['new_schemas']} "
              f"rows_changed={entry['rows_changed']} appended={entry['rows_appended']} "
              f"restored_equal={entry['restored_equal_before']} "
              f"upgrade={json.dumps(entry['upgrade'])[:300]} reopen={json.dumps(entry['reopen'])[:200]}",
              flush=True)
    for name in list(names.values()) + list(restored.values()):
        drop_db(name)
    out.write_text(json.dumps(report, indent=1, sort_keys=True), encoding="utf-8")
    digest = hashlib.sha256(out.read_bytes()).hexdigest()[:16]
    counts = {v: sum(1 for c in report["cases"].values() if c.get("verdict") == v)
              for v in ("PASS", "REFUSED", "FAIL")}
    print(f"DONE {release} {counts['PASS']} pass, {counts['REFUSED']} refused, {counts['FAIL']} fail, "
          f"sha256 {digest}")
    return 0


if __name__ == "__main__":
    if sys.argv[1] == "drive":
        sys.exit(drive(sys.argv[2], sys.argv[3], Path(sys.argv[4])))
    sys.exit(run_step(sys.argv[1], Path(sys.argv[2])))
