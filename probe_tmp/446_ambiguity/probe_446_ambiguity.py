"""#446 ambiguity proof: build the per-revision owner manifest from the code, build representative
overlapping, partial, foreign and derived-schema layouts, and run the read-only preflight prototype
(ownership_proof.py) on each one.

Evidence for a proposal, not the fix. It runs against the checked-out openminion and a PostgreSQL server
at OPENMINION_TEST_POSTGRES_URL (a superuser URL). Every case uses a new database.

For every case it prints:
- STEP: how the layout was built, with what changed;
- LEDGER / TABLE: the ledgers, the complete histories, and the class of every OpenMinion-named table;
- DECIDE: the decision for runtime storage and for memory, a2a, session, registry and artifact, with the
  refusal message;
- UNCHANGED: a whole-database snapshot (schemas, tables, columns, row counts, md5 of all rows, indexes,
  functions, triggers, alembic_version, om_meta, migrations) before and after the preflight, plus
  transaction_read_only and the transaction id the preflight transaction was assigned (none = no write);
- TODAY: for each refused owner, what the current code does on a copy of the same database.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
import traceback
import uuid
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ownership_proof import (  # noqa: E402
    INFRA, Manifest, State, classify, preflight, qi, read_layout,
)

BASE = os.environ["OPENMINION_TEST_POSTGRES_URL"]
# Usage: probe_446_ambiguity.py OUT.json                  (checkout: manifest, cases, preflight)
#        probe_446_ambiguity.py --release TAG OUT.json    (a published release: its manifest and the
#                                                          histories it builds, for the checkout run)
RELEASE = sys.argv[2] if len(sys.argv) > 3 and sys.argv[1] == "--release" else None
OUT = Path(sys.argv[-1]) if len(sys.argv) > 1 else Path("probe_446_ambiguity.json")
ROOT = Path(tempfile.mkdtemp(prefix="p446amb-"))
os.environ["OPENMINION_HOME"] = str(ROOT / "home")
os.environ.pop("PGOPTIONS", None)
SHOWN = ["memory", "a2a", "session", "registry", "artifact"]
RESULTS: dict = {"manifest": {}, "cases": {}, "releases": {}}
MANIFEST: Manifest | None = None  # the checkout's shapes plus the release shapes that differ
DEV_MANIFEST: Manifest | None = None  # the checkout's shapes only
ROLE = f"om_app_{uuid.uuid4().hex[:6]}"
ROLE_PASSWORD = "probe446"


def say(kind: str, case: str, msg: str) -> None:
    print(f"{kind} {case} | {msg}", flush=True)


def short_exc(exc: BaseException) -> str:
    frames = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    loc = f" (at {Path(frames[-1].filename).name}:{frames[-1].lineno})" if frames else ""
    first = str(exc).splitlines()[0][:300] if str(exc) else ""
    return f"{type(exc).__name__}: {first}{loc}"


def attempt(fn) -> str:
    try:
        return "ok: " + str(fn())
    except Exception as exc:  # noqa: BLE001
        return "FAILED " + short_exc(exc)


def url_for(dbname: str, *, user: str | None = None, password: str | None = None) -> str:
    u = sa.engine.make_url(BASE).set(database=dbname)
    if user:
        u = u.set(username=user, password=password)
    return u.render_as_string(hide_password=False)


def admin_engine():
    return sa.create_engine(BASE, isolation_level="AUTOCOMMIT")


def new_db(tag: str) -> str:
    name = f"p446a_{tag}"[:63]
    eng = admin_engine()
    with eng.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {qi(name)}"))
        conn.execute(text(f"CREATE DATABASE {qi(name)}"))
    eng.dispose()
    return url_for(name)


def clone_db(url: str, tag: str) -> str:
    src = sa.engine.make_url(url).database
    name = f"p446t_{tag}"[:63]
    eng = admin_engine()
    with eng.connect() as conn:
        conn.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :d AND pid <> pg_backend_pid()"), {"d": src})
        conn.execute(text(f"DROP DATABASE IF EXISTS {qi(name)}"))
        conn.execute(text(f"CREATE DATABASE {qi(name)} TEMPLATE {qi(src)}"))
    eng.dispose()
    return url_for(name)


def derived_url(url: str, schema: str) -> str:
    u = sa.engine.make_url(url)
    query = dict(u.query)
    existing = query.get("options") or os.environ.get("PGOPTIONS", "")
    query["options"] = f'{existing} -c search_path="{schema}"'.strip()
    return u.set(query=query).render_as_string(hide_password=False)


def admin_exec(url: str, *statements: str) -> str:
    eng = sa.create_engine(url)
    with eng.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))
    eng.dispose()
    return f"{len(statements)} statement(s)"


# ---------------------------------------------------------------- snapshots (as in the round-2 probe)

SYSTEM_SCHEMA = "n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'"


def snapshot(url: str) -> dict:
    eng = sa.create_engine(url)
    snap: dict = {}
    with eng.connect() as conn:
        schemas = [r[0] for r in conn.execute(text(
            f"SELECT n.nspname, pg_get_userbyid(n.nspowner) FROM pg_namespace n WHERE {SYSTEM_SCHEMA} ORDER BY 1"))]
        for schema in schemas:
            rels = conn.execute(text(
                "SELECT c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = :s AND c.relkind IN ('r','p','v','m','S','f') ORDER BY 1"), {"s": schema}).all()
            tables: dict = {}
            for name, kind in rels:
                if kind not in ("r", "p"):
                    continue
                cols = [f"{r[0]} {r[1]}" for r in conn.execute(text(
                    "SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = :s "
                    "AND table_name = :t ORDER BY ordinal_position"), {"s": schema, "t": name})]
                count, md5 = conn.execute(text(
                    f"SELECT count(*), md5(coalesce(string_agg(x::text, '|' ORDER BY x::text), '')) "
                    f"FROM {qi(schema)}.{qi(name)} x")).one()
                tables[name] = {"columns": cols, "rows": int(count), "md5": md5}
            ledgers: dict = {}
            if "alembic_version" in tables:
                ledgers["alembic_version"] = [r[0] for r in conn.execute(text(
                    f"SELECT version_num FROM {qi(schema)}.alembic_version ORDER BY 1"))]
            if "om_meta" in tables:
                ledgers["om_meta"] = {r[0]: r[1] for r in conn.execute(text(
                    f"SELECT key, value FROM {qi(schema)}.om_meta")) if r[0] in ("module_id", "schema_head")}
            if "migrations" in tables and any(c.startswith("version ") for c in tables["migrations"]["columns"]):
                ledgers["migrations"] = [r[0] for r in conn.execute(text(
                    f"SELECT version FROM {qi(schema)}.migrations ORDER BY version"))]
            snap[schema] = {
                "owner": conn.execute(text("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = :s"),
                                      {"s": schema}).scalar(),
                "tables": tables,
                "other_relations": {name: kind for name, kind in rels if kind not in ("r", "p")},
                "functions": sorted(r[0] for r in conn.execute(text(
                    "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE n.nspname = :s"), {"s": schema})),
                "indexes": sorted(f"{r[0]} on {r[1]}" for r in conn.execute(text(
                    "SELECT indexname, tablename FROM pg_indexes WHERE schemaname = :s"), {"s": schema})),
                "triggers": sorted(f"{r[0]} on {r[1]}" for r in conn.execute(text(
                    "SELECT t.tgname, cl.relname FROM pg_trigger t JOIN pg_class cl ON cl.oid = t.tgrelid "
                    "JOIN pg_namespace n ON n.oid = cl.relnamespace WHERE n.nspname = :s AND NOT t.tgisinternal"),
                    {"s": schema})),
                "ledgers": ledgers,
            }
    eng.dispose()
    return snap


def digest(snap: dict) -> str:
    return hashlib.sha256(json.dumps(snap, sort_keys=True).encode()).hexdigest()


def compact(snap: dict) -> dict:
    return {s: {"tables": {n: i["rows"] for n, i in b["tables"].items()}, "ledgers": b["ledgers"]}
            for s, b in snap.items() if b["tables"] or s != "public"}


def snapshot_diff(before: dict, after: dict) -> list:
    changes = []
    for schema in sorted(set(before) | set(after)):
        b, a = before.get(schema), after.get(schema)
        if b is None or a is None:
            changes.append(f"schema {schema} {'added' if b is None else 'removed'}")
            continue
        for name in sorted(set(b["tables"]) | set(a["tables"])):
            tb, ta = b["tables"].get(name), a["tables"].get(name)
            if tb != ta:
                if tb is None or ta is None:
                    changes.append(f"{schema}.{name} {'added' if tb is None else 'removed'}")
                else:
                    what = [k for k in ("columns", "rows", "md5") if tb[k] != ta[k]]
                    added = sorted(set(ta["columns"]) - set(tb["columns"]))
                    changes.append(f"{schema}.{name} changed {what}" + (f" +columns {added}" if added else ""))
        for key in ("functions", "triggers", "ledgers", "other_relations", "owner"):
            if b[key] != a[key]:
                changes.append(f"{schema} {key} {b[key]} -> {a[key]}")
        added = sorted(set(a["indexes"]) - set(b["indexes"]))
        removed = sorted(set(b["indexes"]) - set(a["indexes"]))
        if added or removed:
            changes.append(f"{schema} indexes +{added} -{removed}")
    return changes


# ---------------------------------------------------------------- owners (the current code)

def storage_config(url: str, root: Path):
    from openminion.modules.storage.engine import StorageEngineConfig

    root.mkdir(parents=True, exist_ok=True)
    return StorageEngineConfig(root_dir=root, sqlite_path=root / "state.db", fallback_root=root,
                               record_backend="record.postgres", record_backend_options={"url": url})


def close_quietly(obj) -> None:
    close = getattr(obj, "close", None)
    if callable(close):
        close()


def build_runtime(url: str, tag: str) -> str:
    from openminion.modules.storage.runtime.context import build_runtime_storage

    ctx = build_runtime_storage(ROOT / tag / "state.db", env={}, record_backend="record.postgres",
                                record_backend_options={"url": url})
    try:
        return f"runtime version {ctx.migration_result.current_version}"
    finally:
        ctx.close()


def runtime_rows(url: str) -> str:
    admin_exec(url,
               "INSERT INTO sessions(id, session_key, channel, target) VALUES ('s-probe', 'k-probe', 'cli', 'probe')",
               "INSERT INTO idempotency_keys(method, idempotency_key) VALUES ('probe.method', 'idem-1')")
    return "1 session row, 1 idempotency row"


def memory_store(url: str, tag: str, *, put: str | None = None) -> str:
    from openminion.modules.memory.models import MemoryRecord
    from openminion.modules.memory.storage.postgres.store import PostgresMemoryStore

    store = PostgresMemoryStore(sa.create_engine(url, future=True),
                                database_path=ROOT / tag / "memory" / "memory.db", owns_engine=True)
    try:
        notes = ["built"]
        if put:
            now = "2026-10-29T00:00:00+00:00"
            store.put(MemoryRecord(id=put, scope="agent:a", type="fact", title="t", content=f"c {put}",
                                   created_at=now, updated_at=now))
            notes.append(f"put {put}")
        return "; ".join(notes)
    finally:
        store.close()


def a2a_audit(url: str, tag: str, *, append: str | None = None) -> str:
    from openminion.modules.a2a.models import AuditRecord
    from openminion.modules.a2a.storage.factory import build_a2a_audit_store

    root = ROOT / tag
    store = build_a2a_audit_store(config=storage_config(url, root), audit_root=root / "a2a-audit")
    try:
        if append:
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            store.append_audit(AuditRecord(ts=now, msg_id=append, trace_id="t-probe", from_agent="a", to_agent="b",
                                           to_capability=None, type="request", method="probe.method", status="ok"))
            return f"built; appended {append}"
        return "built"
    finally:
        close_quietly(store)


def a2a_state(url: str, tag: str) -> str:
    from openminion.modules.a2a.storage.factory import build_a2a_state_store

    root = ROOT / tag
    store = build_a2a_state_store(config=storage_config(url, root), database_path=root / "a2a-state.db")
    close_quietly(store)
    return "built"


def session_store(url: str, tag: str) -> str:
    from openminion.modules.session.runtime.factory import build_module_session_store

    root = ROOT / tag
    store = build_module_session_store(config=storage_config(url, root), database_path=root / "sessions.db", env={})
    close_quietly(store)
    return "built"


def alembic_module(url: str, module_id: str, tag: str, target: str = "head") -> str:
    from openminion.modules.storage.migrations.module_ids import get_module_application_id
    from openminion.modules.storage.migrations.runner import MigrationRunner

    eng = sa.create_engine(url)
    try:
        report = MigrationRunner(
            module_id=module_id, db_path=ROOT / tag / f"{module_id}.db",
            module_application_id=get_module_application_id(module_id), backend_type="postgres", engine=eng,
        ).migrate(target=target)
        error = report.error.splitlines()[0][:220] if report.error else None
        return (f"success={report.success} revision {report.before.alembic_revision} -> "
                f"{report.after.alembic_revision}; error={error}")
    finally:
        eng.dispose()


def cli_order() -> list:
    from openminion.modules.storage.migrations.module_ids import MODULE_APPLICATION_IDS
    from openminion.modules.storage.migrations.registry import POSTGRES_VALIDATED_MODULES

    return [m for m in MODULE_APPLICATION_IDS if m in POSTGRES_VALIDATED_MODULES]


def module_chain(module_id: str) -> list:
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from openminion.modules.storage.migrations.alembic import discover_module_alembic_paths

    _, location = discover_module_alembic_paths(module_id)
    if location is None:
        return []
    cfg = Config()
    cfg.set_main_option("script_location", str(location))
    return [s.revision for s in reversed(list(ScriptDirectory.from_config(cfg).walk_revisions()))]


TODAY = {
    "runtime": lambda u, t: build_runtime(u, t),
    "memory": lambda u, t: memory_store(u, t, put="rec-today"),
    "a2a": lambda u, t: a2a_audit(u, t, append="msg-today"),
    "session": lambda u, t: alembic_module(u, "session", t),
    "registry": lambda u, t: alembic_module(u, "registry", t),
    "artifact": lambda u, t: alembic_module(u, "artifact", t),
}


# ---------------------------------------------------------------- manifest

def read_all(url: str, schema: str):
    eng = sa.create_engine(url)
    with eng.connect() as conn:
        lay = read_layout(conn, schema, None)
        conn.rollback()
    eng.dispose()
    return lay


def own_tables(lay) -> dict:
    return {t: s for t, s in lay.tables.items() if t not in INFRA}


def fmt_sig(sig: tuple) -> str:
    cols = ", ".join(f"{n} {t}{' NN' if nn else ''}" for n, t, nn in sig[0])
    return f"({cols}) pk({', '.join(sig[1])})"


def build_manifest(db_tag: str = "manifest") -> Manifest:
    from openminion.modules.storage.backends.postgres import RecordStorePostgres
    from openminion.modules.storage.runtime.migrations import DEFAULT_MIGRATIONS, migrate_record_store

    url = new_db(db_tag)
    states: dict = {"runtime": []}
    versions = {m.version: m.name for m in DEFAULT_MIGRATIONS}
    admin_exec(url, 'CREATE SCHEMA "mf_runtime"')
    store = RecordStorePostgres(derived_url(url, "mf_runtime"))
    try:
        for k in sorted(versions):
            migrate_record_store(store, backend_type="postgres", migrations=DEFAULT_MIGRATIONS[:k])
            lay = read_all(url, "mf_runtime")
            if [v for v, _ in lay.migrations or []] != list(range(1, k + 1)):
                say("WARNING", "manifest", f"runtime ledger after v{k}: {lay.migrations}")
            states["runtime"].append(State("runtime", f"v{k}", "runtime", k, own_tables(lay)))
    finally:
        store.close()
    say("MANIFEST", "runtime", f"v1..v{max(versions)} built one by one: {len(states['runtime'][-1].required)} tables at v{max(versions)}")

    for m in cli_order():
        chain = module_chain(m)
        schema = f"mf_{m}"
        admin_exec(url, f"CREATE SCHEMA {qi(schema)}")
        built = []
        for i, rev in enumerate(chain):
            result = attempt(lambda m=m, rev=rev: alembic_module(derived_url(url, schema), m, f"mf-{m}", target=rev))
            if "success=True" not in result:
                say("MANIFEST", m, f"{rev}: {result}; module left out of the manifest")
                RESULTS["manifest"].setdefault("failed", {})[m] = result
                built = []
                break
            lay = read_all(url, schema)
            if lay.alembic != [rev] or (lay.om_meta or {}).get("module_id") != m:
                say("WARNING", "manifest", f"{m} ledger after {rev}: alembic={lay.alembic} om_meta={lay.om_meta}")
            built.append(State(m, rev, "alembic", i, own_tables(lay)))
        if built:
            states[m] = built
            say("MANIFEST", m, f"{span_labels([s.label for s in built])} built one by one: "
                f"{len(built[-1].required)} tables at {built[-1].label}")

    stores = {"memory_store": ("memory", lambda u: memory_store(u, "mf-ms")),
              "a2a_audit_store": ("a2a", lambda u: a2a_audit(u, "mf-aa")),
              "a2a_state_store": ("a2a", lambda u: a2a_state(u, "mf-as")),
              "session_store": ("session", lambda u: session_store(u, "mf-ss"))}
    for name, (m, build) in stores.items():
        schema = f"mf_{name}"
        admin_exec(url, f"CREATE SCHEMA {qi(schema)}")
        result = attempt(lambda: build(derived_url(url, schema)))
        lay = read_all(url, schema)
        tables = own_tables(lay)
        head = states[m][-1]
        extras = {t: s for t, s in tables.items() if t not in head.required}
        differs = sorted(t for t, s in tables.items() if t in head.required and s != head.required[t])
        say("MANIFEST", name, f"{result} | ledger alembic={lay.alembic} om_meta={lay.om_meta} | "
            f"tables beyond {m}@{head.label}: {sorted(extras)} | same name, other signature than {m}@{head.label}: {differs}")
        if (lay.om_meta or {}).get("module_id") != m:
            say("WARNING", "manifest", f"{name} wrote om_meta={lay.om_meta}")
        for st in states[m]:
            if st.kind == "alembic":
                st.optional.update(extras)
        if not lay.alembic:
            states[m].append(State(m, "store", "store", 1000, tables))
    manifest = Manifest(states=states, runtime_versions=versions)

    report_manifest(manifest, "manifest", "")
    return manifest


def report_manifest(manifest: Manifest, key: str, suffix: str) -> None:
    states = manifest.states
    by_name: dict = {}
    for owner, sts in states.items():
        for st in sts:
            label = "store bootstrap" if st.kind == "store" else st.label
            label = label if st.source == "dev" else f"{label} ({st.source} shape)"
            for t, sig in st.required.items():
                labels = by_name.setdefault(t, {}).setdefault(owner, {}).setdefault(sig, [])
                if label not in labels:
                    labels.append(label)
            for t, sig in st.optional.items():
                labels = by_name.setdefault(t, {}).setdefault(owner, {}).setdefault(sig, [])
                if "store bootstrap" not in labels:
                    labels.append("store bootstrap")
    shared = {t: o for t, o in by_name.items() if len(o) > 1}
    RESULTS.setdefault(key, {})["owners"] = {o: [s.name for s in sts] for o, sts in states.items()}
    RESULTS[key]["tables"] = dump_states(manifest)["states"]
    RESULTS[key]["shared_names"] = {}
    for t in sorted(shared):
        entry = []
        for owner in sorted(shared[t]):
            for sig, labels in shared[t][owner].items():
                entry.append({"owner": owner, "revisions": labels, "signature": fmt_sig(sig)})
                say("SHARED" + suffix, t, f"{owner} {span_labels(labels)}: {fmt_sig(sig)}")
        owners = sorted(shared[t])
        same = []
        for i, a in enumerate(owners):
            for b in owners[i + 1:]:
                for sig in set(shared[t][a]) & set(shared[t][b]):
                    same.append(f"{a} {span_labels(shared[t][a][sig])} == {b} {span_labels(shared[t][b][sig])}")
        say("IDENTICAL" + suffix, t, "; ".join(same) if same else "no two owners share a signature")
        RESULTS[key]["shared_names"][t] = {"signatures": entry, "identical_across_owners": same}


def span_labels(labels: list) -> str:
    return labels[0] if len(labels) == 1 else f"{labels[0]}..{labels[-1]}"


def dump_states(manifest: Manifest) -> dict:
    def sigs(d: dict) -> dict:
        return {t: [[list(c) for c in s[0]], list(s[1])] for t, s in d.items()}

    return {"runtime_versions": {str(k): v for k, v in manifest.runtime_versions.items()},
            "states": {o: [{"label": st.label, "kind": st.kind, "order": st.order, "source": st.source,
                            "required": sigs(st.required), "optional": sigs(st.optional)} for st in sts]
                       for o, sts in manifest.states.items()}}


def load_states(data: dict, source: str) -> Manifest:
    def sigs(d: dict) -> dict:
        return {t: (tuple(tuple(c) for c in s[0]), tuple(s[1])) for t, s in d.items()}

    return Manifest(
        states={o: [State(o, s["label"], s["kind"], s["order"], sigs(s["required"]), sigs(s["optional"]), source)
                    for s in sts] for o, sts in data["states"].items()},
        runtime_versions={int(k): v for k, v in data["runtime_versions"].items()})


def merge_release(dev: Manifest, rel: Manifest, tag: str) -> Manifest:
    """Add every release shape that differs from the checkout's shape of the same revision."""
    states = {o: list(sts) for o, sts in dev.states.items()}
    for v, name in rel.runtime_versions.items():
        if dev.runtime_versions.get(v) != name:
            say("RELEASE", tag, f"runtime v{v} is named {name!r} there and {dev.runtime_versions.get(v)!r} here")
    for owner, sts in rel.states.items():
        same, added = [], []
        for st in sts:
            mine = [d for d in dev.states.get(owner, []) if d.label == st.label and d.kind == st.kind]
            if any(d.required == st.required and d.optional == st.optional for d in mine):
                same.append(st.label)
                continue
            states.setdefault(owner, []).append(st)
            if not mine:
                added.append(f"{st.label}: not in the checkout")
                continue
            d = mine[0]
            only_rel = sorted(set(st.required) - set(d.required))
            only_dev = sorted(set(d.required) - set(st.required))
            other = sorted(t for t in set(st.required) & set(d.required) if st.required[t] != d.required[t])
            added.append(f"{st.label}: tables only in {tag} {only_rel}, only in the checkout {only_dev}, "
                         f"other signature {other}")
        say("RELEASE", tag, f"{owner}: same shape as the checkout for {span_labels(same) if same else 'no revision'}"
            + (f"; added as extra shapes: {'; '.join(added)}" if added else ""))
        RESULTS["releases"].setdefault(tag, {})[owner] = {"same": same, "added": added}
    return Manifest(states=states, runtime_versions=dev.runtime_versions)


# ---------------------------------------------------------------- cases

def run_preflight(admin_url: str, run_url: str, case: str, label: str, manifest: Manifest | None = None) -> dict:
    before = snapshot(admin_url)
    eng = sa.create_engine(run_url)
    with eng.connect() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        read_only = conn.execute(text("SHOW transaction_read_only")).scalar()
        out = preflight(conn, manifest or MANIFEST, SHOWN)
        conn.rollback()
    eng.dispose()
    after = snapshot(admin_url)
    out["transaction_read_only"] = read_only
    out["unchanged"] = digest(before) == digest(after)
    out["digest"] = digest(before)
    out["state"] = compact(before)
    tag = f"{case}{label}"
    say("STATE", tag, json.dumps(out["state"], sort_keys=True))
    for schema, line in out.get("ledgers", {}).items():
        say("LEDGER", tag, f"{schema} | {line}")
    for schema, tables in out.get("classes", {}).items():
        for t, info in tables.items():
            owner = f"({info['owner']})" if info["owner"] else ""
            say("TABLE", tag, f"{schema}.{t} | {info['cls']}{owner} | {info['detail']}")
    for owner, (action, message) in out["decisions"].items():
        say("DECIDE", tag, f"{owner}: {action} | {message}")
    say("UNCHANGED", tag, f"database identical before/after: {out['unchanged']} (sha256 {out['digest'][:16]}) | "
        f"transaction_read_only={read_only} | transaction id assigned: {out.get('xid')} | role {out.get('role')} | "
        f"ledgers: {json.dumps({s: b['ledgers'] for s, b in out['state'].items()}, sort_keys=True)}")
    out["decisions"] = {o: list(d) for o, d in out["decisions"].items()}
    return out


def run_case(case: str, title: str, url: str, steps: list, *, as_url: str | None = None, today: bool = True,
             after: list | None = None, dev_only_first: bool = False, recorded: list | None = None) -> None:
    say("CASE", case, title)
    body: dict = {"title": title, "steps": list(recorded or [])}
    for item in recorded or []:
        say("STEP", case, f"{item['step']}: {item['result']}")
    for name, fn in steps:
        before = snapshot(url)
        result = attempt(fn)
        changes = snapshot_diff(before, snapshot(url))
        body["steps"].append({"step": name, "result": result, "changes": changes})
        failed = result.startswith("FAILED") or "success=False" in result
        say("STEP", case, f"{name}: {result}" + (f" | left behind: {changes}" if failed else ""))
    if dev_only_first:
        body["preflight_dev_manifest_only"] = run_preflight(url, as_url or url, case, "_dev_manifest_only", DEV_MANIFEST)
    body["preflight"] = run_preflight(url, as_url or url, case, "")
    if today:
        body["today"] = {}
        for owner, (action, _) in body["preflight"]["decisions"].items():
            if action != "REFUSE" or owner not in TODAY:
                continue
            clone = clone_db(url, f"{case}_{owner}")
            before = snapshot(clone)
            result = attempt(lambda: TODAY[owner](clone, f"today-{case}-{owner}"))
            changes = snapshot_diff(before, snapshot(clone))
            body["today"][owner] = {"result": result, "changes": changes}
            say("TODAY", case, f"{owner} (current code, on a copy): {result} | changes {changes}")
    for name, fn, label in after or []:
        result = attempt(fn)
        say("STEP", case + label, f"{name}: {result}")
        body.setdefault("after", []).append({"step": name, "result": result,
                                             "preflight": run_preflight(url, as_url or url, case, label)})
    RESULTS["cases"][case] = body


SHARED_DDL = (
    """CREATE TABLE session_turn_leases (
        session_id TEXT PRIMARY KEY, owner TEXT NOT NULL, request_id TEXT NOT NULL, fence_token INTEGER NOT NULL,
        acquired_at TEXT NOT NULL, renewed_at TEXT NOT NULL, expires_at TEXT NOT NULL, released_at TEXT)""",
    """CREATE TABLE session_retention_holds (
        hold_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, reason TEXT NOT NULL, actor_id TEXT NOT NULL,
        created_at TEXT NOT NULL, released_at TEXT, schema_version TEXT NOT NULL DEFAULT 'session_retention_hold.v1')""",
    "INSERT INTO session_turn_leases VALUES ('s-1', 'worker', 'r-1', 1, '2026-10-29', '2026-10-29', '2026-10-30', NULL)",
    "INSERT INTO session_retention_holds(hold_id, session_id, reason, actor_id, created_at) "
    "VALUES ('h-1', 's-1', 'legal', 'ops', '2026-10-29')",
)


def cases() -> None:
    admin = admin_engine()
    with admin.connect() as conn:
        conn.execute(text(f"CREATE ROLE {qi(ROLE)} LOGIN PASSWORD '{ROLE_PASSWORD}'"))
    admin.dispose()

    # (a) one unambiguous, fully matching history per owner
    u = new_db("a_runtime")
    run_case("a_runtime", "runtime storage v1..v15 with one session and one idempotency row", u,
             [("runtime storage", lambda: build_runtime(u, "a-rt")), ("runtime rows", lambda: runtime_rows(u))])
    u = new_db("a_memory")
    run_case("a_memory", "dedicated memory history (memory store, one record)", u,
             [("memory store + record", lambda: memory_store(u, "a-m", put="rec-1"))])
    u = new_db("a_a2a")
    run_case("a_a2a", "dedicated A2A history (A2A audit store, one record)", u,
             [("a2a audit store + record", lambda: a2a_audit(u, "a-a", append="msg-1"))])
    u = new_db("a_session")
    run_case("a_session", "dedicated session history (storage migrate --module session)", u,
             [("migrate session", lambda: alembic_module(u, "session", "a-s"))])
    u = new_db("a_registry")
    run_case("a_registry", "dedicated registry history (storage migrate --module registry, one agent)", u,
             [("migrate registry", lambda: alembic_module(u, "registry", "a-r")),
              ("registry row", lambda: admin_exec(u, "INSERT INTO agents(agent_id, descriptor_json, source, updated_at) "
                                                     "VALUES ('agent-1', '{}', 'probe', '2026-10-29')"))])
    u = new_db("a_memory_0002")
    run_case("a_memory_0002", "historical memory history: migrated to 0002_audit only, one record", u,
             [("migrate memory to 0002_audit", lambda: alembic_module(u, "memory", "a-m2", target="0002_audit")),
              ("memory row", lambda: admin_exec(u, "INSERT INTO memory_records(id, scope, type, content_json, source, "
                                                   "confidence, created_at, updated_at) VALUES ('rec-1', 'agent:a', "
                                                   "'fact', '{}', 'probe', 1.0, '2026-10-29', '2026-10-29')"))])

    # (b) overlapping: the columns equal revisions of two owners
    u = new_db("b_shared")
    run_case("b_shared", "session_turn_leases and session_retention_holds alone (as a partial restore leaves them), "
             "with the column sets runtime storage and the session module both create; no ledger", u,
             [("create the two tables with one row each", lambda: admin_exec(u, *SHARED_DDL))])
    u = new_db("b_mem_rt")
    run_case("b_mem_rt", "memory store, then runtime storage (fails at v4): migrations is in both histories", u,
             [("memory store + record", lambda: memory_store(u, "b-mr", put="rec-1")),
              ("runtime storage", lambda: build_runtime(u, "b-mr"))])
    u = new_db("b_rt_a2a")
    run_case("b_rt_a2a", "runtime storage with rows, then the A2A audit store with a record (round-2 row 5: A2A adopts "
             "runtime storage's idempotency_keys)", u,
             [("runtime storage", lambda: build_runtime(u, "b-ra")), ("runtime rows", lambda: runtime_rows(u)),
              ("a2a audit store + record", lambda: a2a_audit(u, "b-ra", append="msg-1"))])
    u = new_db("b_rt_sstore")
    run_case("b_rt_sstore", "runtime storage with rows, then the session module store (fails; leaves an index on "
             "runtime storage's sessions)", u,
             [("runtime storage", lambda: build_runtime(u, "b-rs")), ("runtime rows", lambda: runtime_rows(u)),
              ("session module store", lambda: session_store(u, "b-rs"))])
    u = new_db("b_lone")
    run_case("b_lone", "A2A audit store, then runtime storage (fails at v1): an empty migrations table is left", u,
             [("a2a audit store + record", lambda: a2a_audit(u, "b-l", append="msg-1")),
              ("runtime storage", lambda: build_runtime(u, "b-l"))])

    # (c) partial or altered tables
    u = new_db("c_sessions")
    run_case("c_sessions", "partial sessions: 4 of runtime storage's v1 columns, one row; no ledger", u,
             [("create partial sessions", lambda: admin_exec(
                 u, "CREATE TABLE sessions (id TEXT PRIMARY KEY, session_key TEXT NOT NULL UNIQUE, "
                    "channel TEXT NOT NULL, target TEXT NOT NULL)",
                 "INSERT INTO sessions VALUES ('s-1', 'k-1', 'cli', 'x')"))])
    u = new_db("c_agents")
    run_case("c_agents", "partial agents(agent_id, updated_at): a column subset of both A2A's and registry's agents", u,
             [("create partial agents", lambda: admin_exec(
                 u, "CREATE TABLE agents (agent_id TEXT PRIMARY KEY, updated_at TEXT NOT NULL)",
                 "INSERT INTO agents VALUES ('agent-1', '2026-10-29')"))])
    u = new_db("c_ahead")
    run_case("c_ahead", "memory tables at 0002_audit with a ledger saying 0004_namespace (as a mixed restore leaves it)", u,
             [("migrate memory to 0002_audit", lambda: alembic_module(u, "memory", "c-a", target="0002_audit")),
              ("ledger rows from a newer dump", lambda: admin_exec(
                  u, "UPDATE alembic_version SET version_num = '0004_namespace'",
                  "UPDATE om_meta SET value = '0004_namespace' WHERE key = 'schema_head'"))])
    u = new_db("c_altered")
    run_case("c_altered", "runtime storage, then --module artifact, then --module memory: memory skips its baseline "
             "and alters runtime storage's memory_records", u,
             [("runtime storage", lambda: build_runtime(u, "c-al")), ("runtime rows", lambda: runtime_rows(u)),
              ("migrate artifact", lambda: alembic_module(u, "artifact", "c-al")),
              ("migrate memory", lambda: alembic_module(u, "memory", "c-al"))])

    # (d) foreign tables with OpenMinion names
    u = new_db("d_foreign")
    run_case("d_foreign", "another application's sessions and idempotency_keys (unrelated columns) and customers", u,
             [("create the other application's tables", lambda: admin_exec(
                 u, "CREATE TABLE sessions (id BIGINT PRIMARY KEY, payload JSONB NOT NULL)",
                 "CREATE TABLE idempotency_keys (token UUID PRIMARY KEY, expires_at TIMESTAMPTZ)",
                 "CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT)",
                 "INSERT INTO sessions VALUES (1, '{}')",
                 "INSERT INTO idempotency_keys VALUES ('6f1c2a52-8d55-4a8f-9a3e-2f6f3c1b9d10', now())",
                 "INSERT INTO customers VALUES (1, 'c')"))])

    # (e) derived schemas
    u = new_db("e_ok")
    db = sa.engine.make_url(u).database
    role_url = url_for(db, user=ROLE, password=ROLE_PASSWORD)
    run_case("e_ok", f"empty public__memory created by a DBA with AUTHORIZATION {ROLE}; preflight as {ROLE}, "
             "a role without CREATE on the database or on public", u,
             [("DBA creates the schema", lambda: admin_exec(u, f'CREATE SCHEMA "public__memory" AUTHORIZATION {qi(ROLE)}'))],
             as_url=role_url, today=False,
             after=[(f"memory store in public__memory as {ROLE} + record",
                     lambda: memory_store(derived_url(role_url, "public__memory"), "e-ok", put="rec-1"), "_after_store")])
    u = new_db("e_nogrant")
    role_url2 = url_for(sa.engine.make_url(u).database, user=ROLE, password=ROLE_PASSWORD)
    run_case("e_nogrant", f"empty public__memory created by the superuser, no grant to {ROLE}; preflight as {ROLE}", u,
             [("superuser creates the schema", lambda: admin_exec(u, 'CREATE SCHEMA "public__memory"'))],
             as_url=role_url2, today=False)
    u = new_db("e_both")
    run_case("e_both", "dedicated memory history in public and an empty public__memory", u,
             [("memory store + record in public", lambda: memory_store(u, "e-b", put="rec-1")),
              ("DBA creates the schema", lambda: admin_exec(u, f'CREATE SCHEMA "public__memory" AUTHORIZATION {qi(ROLE)}'))],
             today=False)
    u = new_db("e_foreign")
    run_case("e_foreign", "public__memory holding another application's table", u,
             [("create schema and table", lambda: admin_exec(
                 u, 'CREATE SCHEMA "public__memory"', 'CREATE TABLE "public__memory".notes (id INTEGER PRIMARY KEY)'))],
             today=False)

    # (r) histories written by a published release, classified with and without that release's shapes
    for tag, built in RELEASE_CASES:
        for case, info in built.items():
            run_case(case, info["title"], url_for(info["db"]), [], today=False, dev_only_first=True,
                     recorded=[dict(item, step=f"[{tag} run] {item['step']}") for item in info["steps"]])


RELEASE_CASES: list = []


def release_mode(tag: str) -> None:
    t = tag.replace(".", "")
    manifest = build_manifest(f"manifest_{t}")
    RESULTS["release_manifest"] = dump_states(manifest)
    RESULTS["release_cases"] = {}

    def record(case: str, title: str, url: str, steps: list) -> None:
        done = []
        for name, fn in steps:
            result = attempt(fn)
            done.append({"step": name, "result": result})
            say("STEP", case, f"{name}: {result}")
        RESULTS["release_cases"][case] = {"db": sa.engine.make_url(url).database, "title": title, "steps": done}

    u = new_db(f"r_session_{t}")
    record(f"r_session_{t}", f"session history written by the published {tag} (storage migrate --module session), "
           "one session row", u,
           [(f"migrate session with {tag}", lambda: alembic_module(u, "session", f"r-s-{t}")),
            ("one session row", lambda: admin_exec(u, "INSERT INTO sessions(session_id, created_at, updated_at, status) "
                                                      "VALUES ('s-1', '2026-10-29', '2026-10-29', 'active')"))])
    u2 = new_db(f"r_runtime_{t}")
    record(f"r_runtime_{t}", f"runtime storage history written by the published {tag}, one session and one "
           "idempotency row", u2,
           [(f"runtime storage with {tag}", lambda: build_runtime(u2, f"r-r-{t}")), ("runtime rows", lambda: runtime_rows(u2))])


def main() -> int:
    global MANIFEST, DEV_MANIFEST
    import importlib.metadata as md

    RESULTS["versions"] = {p: md.version(p) for p in ("openminion", "sqlalchemy", "psycopg", "alembic")}
    say("VERSIONS", "packages", json.dumps(RESULTS["versions"]))
    eng = admin_engine()
    with eng.connect() as conn:
        RESULTS["server"] = conn.execute(text("SHOW server_version")).scalar()
    eng.dispose()
    say("VERSIONS", "postgres", RESULTS["server"])
    try:
        if RELEASE:
            release_mode(RELEASE)
            OUT.write_text(json.dumps(RESULTS, indent=1, sort_keys=True, default=str), encoding="utf-8")
            return 0
        MANIFEST = DEV_MANIFEST = build_manifest()
        for item in filter(None, os.environ.get("EXTRA_MANIFESTS", "").split(",")):
            tag, path = item.split("=", 1)
            if not Path(path).is_file():
                say("RELEASE", tag, f"no release output at {path}; release shapes not merged")
                continue
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if "release_manifest" not in data:
                say("RELEASE", tag, f"release run has no manifest: {data.get('error', '')[-300:]}")
                continue
            MANIFEST = merge_release(MANIFEST, load_states(data["release_manifest"], tag), tag)
            RELEASE_CASES.append((tag, data.get("release_cases", {})))
        if MANIFEST is not DEV_MANIFEST:
            report_manifest(MANIFEST, "manifest_with_releases", "_WITH_RELEASES")
        cases()
    except Exception as exc:  # noqa: BLE001
        say("ERROR", "main", short_exc(exc))
        RESULTS["error"] = traceback.format_exc()
        print(RESULTS["error"], flush=True)
    OUT.write_text(json.dumps(RESULTS, indent=1, sort_keys=True, default=str), encoding="utf-8")
    return 1 if "error" in RESULTS else 0


if __name__ == "__main__":
    sys.exit(main())
