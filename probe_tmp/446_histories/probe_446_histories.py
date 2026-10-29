"""#446: measure the PostgreSQL layout every OpenMinion owner creates, with the installed package.

Run it with the Python of a venv where `openminion[postgres]==X` was installed from PyPI (or of a
checkout), from an empty directory:

    python -I -X utf8 probe_446_histories.py LABEL OUT.json

OPENMINION_TEST_POSTGRES_URL must be a superuser URL. Every case uses a new database, which is dropped
after it was read. For every owner the probe records what it has after each step:
- runtime storage: `migrate_record_store` with v1..vk, one version at a time, and one full
  `build_runtime_storage` start;
- every module with an Alembic chain: `MigrationRunner.migrate(target=<revision>)`, one revision at a time;
- every PostgreSQL module store the package has: built alone on a new database, and built after its
  module's Alembic head on the same database.

For each table: its schema, its exact signature (column names, `format_type`, NOT NULL, primary key
columns). For each schema: the ledgers (`alembic_version` rows, `om_meta`, the `(version, name)` rows of
`migrations`), functions and triggers. Failures are recorded, not hidden. The probe is evidence for #446,
not the implementation.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
import uuid
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import text

BASE = os.environ["OPENMINION_TEST_POSTGRES_URL"]
LABEL = sys.argv[1]
OUT = Path(sys.argv[2])
ROOT = Path(tempfile.mkdtemp(prefix="p446h-"))
os.environ["OPENMINION_HOME"] = str(ROOT / "home")
os.environ["OPENMINION_DATA_ROOT"] = str(ROOT / "data")
os.environ.pop("PGOPTIONS", None)
os.environ.setdefault("OPENMINION_SECRET_KEY", "ZmFrZS1rZXktZm9yLXRoZS00NDYtcHJvYmUtb25seS0=")
SYSTEM = "n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'"
TOKEN = uuid.uuid4().hex[:6]
RESULT: dict = {"label": LABEL, "owners": {}, "errors": []}


def say(*parts: object) -> None:
    print(" ".join(str(p) for p in parts), flush=True)


def short_exc(exc: BaseException) -> str:
    frames = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    where = f" (at {Path(frames[-1].filename).name}:{frames[-1].lineno})" if frames else ""
    first = str(exc).splitlines()[0][:300] if str(exc) else ""
    return f"{type(exc).__name__}: {first}{where}"


def qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def admin():
    return sa.create_engine(BASE, isolation_level="AUTOCOMMIT")


def new_db(tag: str) -> str:
    name = f"h446_{TOKEN}_{tag}"[:63]
    eng = admin()
    with eng.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {qi(name)}"))
        conn.execute(text(f"CREATE DATABASE {qi(name)}"))
    eng.dispose()
    return sa.engine.make_url(BASE).set(database=name).render_as_string(hide_password=False)


def drop_db(url: str) -> None:
    name = sa.engine.make_url(url).database
    eng = admin()
    with eng.connect() as conn:
        conn.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                          "WHERE datname = :d AND pid <> pg_backend_pid()"), {"d": name})
        conn.execute(text(f"DROP DATABASE IF EXISTS {qi(name)}"))
    eng.dispose()


def read_db(url: str) -> dict:
    """Every non-system schema: tables with exact signatures, ledgers, functions, triggers."""
    eng = sa.create_engine(url)
    out: dict = {}
    with eng.connect() as conn:
        schemas = [r[0] for r in conn.execute(text(
            f"SELECT n.nspname FROM pg_namespace n WHERE {SYSTEM} ORDER BY 1"))]
        for schema in schemas:
            oid = conn.execute(text("SELECT oid FROM pg_namespace WHERE nspname = :s"), {"s": schema}).scalar()
            cols: dict = {}
            for rel, att, typ, notnull in conn.execute(text(
                    "SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull "
                    "FROM pg_class c JOIN pg_attribute a ON a.attrelid = c.oid "
                    "WHERE c.relnamespace = :o AND c.relkind IN ('r', 'p') AND a.attnum > 0 "
                    "AND NOT a.attisdropped"), {"o": oid}):
                cols.setdefault(rel, []).append([att, typ, bool(notnull)])
            pks: dict = {}
            for rel, att in conn.execute(text(
                    "SELECT c.relname, a.attname FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid "
                    "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey) "
                    "WHERE i.indisprimary AND c.relnamespace = :o"), {"o": oid}):
                pks.setdefault(rel, []).append(att)
            tables = {t: {"columns": sorted(c), "pk": sorted(pks.get(t, []))} for t, c in sorted(cols.items())}
            ledgers: dict = {}
            names = {c[0] for c in cols.get("alembic_version", [])}
            if "version_num" in names:
                ledgers["alembic_version"] = sorted(str(r[0]) for r in conn.execute(text(
                    f"SELECT version_num FROM {qi(schema)}.alembic_version")))
            if {"key", "value"} <= {c[0] for c in cols.get("om_meta", [])}:
                ledgers["om_meta"] = {str(r[0]): str(r[1]) for r in conn.execute(text(
                    f"SELECT key, value FROM {qi(schema)}.om_meta")) if r[0] != "last_migrated_at"}
            if {"version", "name"} <= {c[0] for c in cols.get("migrations", [])}:
                ledgers["migrations"] = [[int(r[0]), str(r[1])] for r in conn.execute(text(
                    f"SELECT version, name FROM {qi(schema)}.migrations ORDER BY version"))]
            functions = sorted(r[0] for r in conn.execute(text(
                "SELECT p.proname FROM pg_proc p WHERE p.pronamespace = :o"), {"o": oid}))
            triggers = sorted(f"{r[0]} on {r[1]}" for r in conn.execute(text(
                "SELECT t.tgname, c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                "WHERE c.relnamespace = :o AND NOT t.tgisinternal"), {"o": oid}))
            if tables or functions or schema != "public":
                out[schema] = {"tables": tables, "ledgers": ledgers, "functions": functions, "triggers": triggers}
    eng.dispose()
    return out


def record(owner: str, step: str, result: str, layout: dict | None) -> None:
    RESULT["owners"].setdefault(owner, []).append({"step": step, "result": result, "layout": layout})
    tables = {s: sorted(b["tables"]) for s, b in (layout or {}).items()}
    say("STEP", owner, step, "|", result, "|", json.dumps(tables, sort_keys=True)[:600])


def attempt(fn) -> str:
    try:
        value = fn()
        return "ok" + (f": {value}" if value else "")
    except Exception as exc:  # noqa: BLE001
        return "FAILED " + short_exc(exc)


def storage_config(url: str, root: Path):
    from openminion.modules.storage.engine import StorageEngineConfig

    root.mkdir(parents=True, exist_ok=True)
    return StorageEngineConfig(root_dir=root, sqlite_path=root / "state.db", fallback_root=root,
                               record_backend="record.postgres", record_backend_options={"url": url})


def close_quietly(obj) -> None:
    close = getattr(obj, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- runtime storage

def measure_runtime() -> None:
    from openminion.modules.storage.backends.postgres import RecordStorePostgres
    from openminion.modules.storage.runtime.migrations import DEFAULT_MIGRATIONS, migrate_record_store

    RESULT["runtime_versions"] = {str(m.version): m.name for m in DEFAULT_MIGRATIONS}
    url = new_db("rt")
    store = RecordStorePostgres(url)
    try:
        ordered = sorted(DEFAULT_MIGRATIONS, key=lambda m: m.version)
        for k in range(1, len(ordered) + 1):
            result = attempt(lambda k=k: migrate_record_store(store, backend_type="postgres",
                                                               migrations=ordered[:k]) and None)
            record("runtime", f"v{ordered[k - 1].version}", result, read_db(url))
            if result.startswith("FAILED"):
                break
    finally:
        store.close()
        drop_db(url)

    url = new_db("rtfull")
    try:
        def build() -> str:
            from openminion.modules.storage.runtime.context import build_runtime_storage

            ctx = build_runtime_storage(ROOT / "rtfull" / "state.db", env={}, record_backend="record.postgres",
                                        record_backend_options={"url": url})
            try:
                return f"current_version={ctx.migration_result.current_version}"
            finally:
                ctx.close()

        record("runtime_storage_start", "build_runtime_storage", attempt(build), read_db(url))
    finally:
        drop_db(url)


# ---------------------------------------------------------------- Alembic modules

def module_ids() -> list:
    from openminion.modules.storage.migrations.module_ids import MODULE_APPLICATION_IDS

    return [m for m in MODULE_APPLICATION_IDS if m != "storage"]


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


def migrate_module(url: str, module_id: str, target: str) -> str:
    from openminion.modules.storage.migrations.module_ids import get_module_application_id
    from openminion.modules.storage.migrations.runner import MigrationRunner

    import inspect

    eng = sa.create_engine(url)
    kwargs = {}
    if "select_postgres_schema" in inspect.signature(MigrationRunner).parameters:
        # Code with #446 puts the module in its own schema, as `storage migrate` does.
        kwargs["select_postgres_schema"] = True
    try:
        report = MigrationRunner(
            module_id=module_id, db_path=ROOT / "mod" / f"{module_id}.db",
            module_application_id=get_module_application_id(module_id), backend_type="postgres", engine=eng,
            **kwargs,
        ).migrate(target=target)
        if not report.success:
            raise RuntimeError(str(report.error).splitlines()[0][:300] if report.error else "migration failed")
        return f"{report.before.alembic_revision} -> {report.after.alembic_revision}"
    finally:
        eng.dispose()


def measure_modules() -> None:
    RESULT["chains"] = {}
    for m in module_ids():
        try:
            chain = module_chain(m)
        except Exception as exc:  # noqa: BLE001
            RESULT["chains"][m] = "FAILED " + short_exc(exc)
            continue
        RESULT["chains"][m] = chain
        if not chain:
            continue
        url = new_db(f"m_{m}")
        try:
            for rev in chain:
                result = attempt(lambda rev=rev: migrate_module(url, m, rev))
                record(f"alembic:{m}", rev, result, read_db(url))
                if result.startswith("FAILED"):
                    break
        finally:
            drop_db(url)


# ---------------------------------------------------------------- stores

def _memory_store(url: str, root: Path):
    from openminion.modules.memory.storage.postgres.store import PostgresMemoryStore

    return PostgresMemoryStore(sa.create_engine(url, future=True), database_path=root / "memory.db",
                               owns_engine=True)


def _a2a_audit(url: str, root: Path):
    from openminion.modules.a2a.storage.factory import build_a2a_audit_store

    return build_a2a_audit_store(config=storage_config(url, root), audit_root=root / "a2a-audit")


def _factory(module: str, name: str, **kwargs):
    def build(url: str, root: Path):
        import importlib

        fn = getattr(importlib.import_module(module), name)
        return fn(config=storage_config(url, root), **{k: (root / v if k == "database_path" else v)
                                                      for k, v in kwargs.items()})

    return build


def _secret_store(url: str, root: Path):
    from openminion.modules.secret.storage.store import PostgresSecretStore
    from openminion.modules.storage.backends.postgres import RecordStorePostgres

    return PostgresSecretStore(record_store=RecordStorePostgres(url))


STORES = {
    # store name: (module whose Alembic chain it shares, builder)
    "memory_store": ("memory", _memory_store),
    "a2a_audit_store": ("a2a", _a2a_audit),
    "a2a_state_store": ("a2a", _factory("openminion.modules.a2a.storage.factory", "build_a2a_state_store",
                                        database_path="a2a-state.db")),
    "session_store": ("session", _factory("openminion.modules.session.runtime.factory",
                                          "build_module_session_store", database_path="sessions.db", env={})),
    "controlplane_store": ("controlplane", _factory("openminion.modules.controlplane.storage",
                                                    "build_controlplane_store", database_path="cp.db")),
    "registry_store": ("registry", _factory("openminion.modules.registry.storage", "build_registry_store",
                                            database_path="registry.db")),
    "artifact_index": ("artifact", _factory("openminion.modules.artifact.storage", "build_artifact_index",
                                            database_path="artifact.db")),
    "identity_store": ("identity", _factory("openminion.modules.identity.storage", "build_identity_store",
                                            database_path="identity.db")),
    "policy_store": ("policy", _factory("openminion.modules.policy.storage", "build_policy_store",
                                        database_path="policy.db")),
    "retrieve_store": ("retrieve", _factory("openminion.modules.retrieve.storage", "build_retrieve_store",
                                            database_path="retrieve.db")),
    "skill_store": ("skill", _factory("openminion.modules.skill.storage", "build_skill_store",
                                      database_path="skill.db")),
    "task_store": ("task", _factory("openminion.modules.task.storage", "build_task_store",
                                    database_path="task.db")),
    "telemetry_store": ("telemetry", _factory("openminion.modules.telemetry.storage", "build_telemetry_store",
                                              database_path="telemetry.db")),
    "compress_telemetry_store": ("compress", _factory("openminion.modules.context.compress.storage",
                                                      "build_compress_telemetry_store",
                                                      database_path="compress.db")),
    "secret_store": ("secret", _secret_store),
}


def measure_stores() -> None:
    # Each store alone; after its module's Alembic head; and followed by the Alembic head
    # (`storage migrate` on a database the store created).
    for name, (module_id, build) in STORES.items():
        chain = RESULT.get("chains", {}).get(module_id)
        has_chain = isinstance(chain, list) and bool(chain)
        for order in ("alone", "after_alembic_head", "alembic_head_after_store"):
            if order != "alone" and not has_chain:
                continue
            tag = f"s_{name}_{order[:5]}"
            url = new_db(tag)
            root = ROOT / tag
            try:
                steps = []
                holder: list = []

                def make() -> str:
                    holder.append(build(url, root))
                    return type(holder[-1]).__name__

                if order == "after_alembic_head":
                    head = attempt(lambda: migrate_module(url, module_id, "head"))
                    steps.append(f"alembic head: {head}")
                    if head.startswith("FAILED"):
                        record(f"store:{name}", order, "; ".join(steps), read_db(url))
                        continue
                steps.append("store: " + attempt(make))
                for obj in holder:
                    close_quietly(obj)
                if order == "alembic_head_after_store":
                    steps.append("alembic head: " + attempt(lambda: migrate_module(url, module_id, "head")))
                record(f"store:{name}", order, "; ".join(steps), read_db(url))
            except Exception as exc:  # noqa: BLE001
                record(f"store:{name}", order, "FAILED " + short_exc(exc), None)
            finally:
                drop_db(url)


def main() -> int:
    import openminion

    RESULT["version"] = getattr(openminion, "__version__", "?")
    RESULT["file"] = str(Path(openminion.__file__).parent).replace(str(Path.home()), "<HOME>")
    RESULT["versions"] = {"sqlalchemy": sa.__version__}
    try:
        import alembic
        import psycopg

        RESULT["versions"].update(alembic=alembic.__version__, psycopg=psycopg.__version__)
    except Exception as exc:  # noqa: BLE001
        RESULT["errors"].append("versions: " + short_exc(exc))
    say("VERSIONS", LABEL, RESULT["version"], json.dumps(RESULT["versions"]))
    for part in (measure_runtime, measure_modules, measure_stores):
        try:
            part()
        except Exception as exc:  # noqa: BLE001
            RESULT["errors"].append(f"{part.__name__}: {short_exc(exc)}")
            say("ERROR", part.__name__, short_exc(exc))
    OUT.write_text(json.dumps(RESULT, indent=1, sort_keys=True), encoding="utf-8")
    say("DONE", LABEL, "owners", len(RESULT["owners"]), "errors", len(RESULT["errors"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
