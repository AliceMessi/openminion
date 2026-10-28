"""`controlplane` and `telemetry` baselines use SQLite-only DDL on PostgreSQL.

Usage: python -I -X utf8 probe_postgres_autoincrement_baseline.py <out_dir> <label>

Needs PGURL (a PostgreSQL server URL, e.g. postgresql+psycopg://user:pw@host/postgres).
Every PostgreSQL case uses a new, empty database; every SQLite case a new file in
a new temporary directory. The CLI is the `openminion` console script next to
this interpreter. Passwords are masked in all output.

Cases:
- ddl_scan: every installed `*/storage/migrations/versions/*.py`: lines with
  AUTOINCREMENT, and every `DDL` tuple a revision applies (with what it imports,
  such as the pairing schema), with or without a PostgreSQL variant.
- pg_cli: `storage migrate --backend postgres --module controlplane` and
  `--module telemetry` (json and text), the same migration in process with the
  CLI's own helpers, and the database afterwards.
- pg_control: `--module secret`, the same way.
- pg_each_module_alone: each module the CLI runs on PostgreSQL, alone, on its own
  new database.
- pg_full_run: `storage migrate --backend postgres` (every module) on one database.
- pg_statements: each statement of the two baselines run on its own on a new
  database; then the same with the replacement the PostgreSQL controlplane store
  already applies (`INTEGER PRIMARY KEY AUTOINCREMENT` -> `SERIAL PRIMARY KEY`).
- patched_copy: a scratch copy of each module's migrations whose 0001 upgrade()
  uses PostgreSQL types on PostgreSQL only; migrated to head on PostgreSQL, and on
  SQLite compared with the shipped scripts (new file and already-migrated file).
- sqlite: `--sqlite <file> --module controlplane` / `--module telemetry`.
- startup_check: `build_runtime_storage` with the PostgreSQL record backend on a
  new database (warnings captured), then the commands its warnings suggest.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

OUT = Path(sys.argv[1])
LABEL = sys.argv[2] if len(sys.argv) > 2 else "x"
PGURL = os.environ.get("PGURL", "")
WORK = Path(tempfile.mkdtemp(prefix="pg-autoincrement-"))
os.environ["OPENMINION_HOME"] = str(WORK / "home")
os.environ["OPENMINION_DATA_ROOT"] = str(WORK / "data")
OM = str(Path(sys.executable).parent / "openminion")
MODULES = ("controlplane", "telemetry")
SERIAL = ("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
RESULTS: dict = {"label": LABEL, "cases": {}}

# Appended to the scratch copy of each 0001_baseline.py: a later `def upgrade`
# replaces the shipped one. SQLite gets the shipped DDL unchanged.
PATCH = '''

def upgrade() -> None:  # probe patch (scratch copy only)
    from alembic import op

    statements = DDL
    if op.get_bind().dialect.name == "postgresql":
        statements = tuple(
            str(s)
            .replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
            .replace("timestamp REAL NOT NULL", "timestamp DOUBLE PRECISION NOT NULL")
            for s in DDL
        )
    apply_ddl_statements(statements)
'''


def mask(text: str) -> str:
    text = re.sub(r"(postgresql(\+[a-z0-9]+)?://[^:/@\s]+):[^@\s]*@", r"\1:***@", str(text))
    return text.replace(str(WORK), "<WORK>").replace(sys.prefix, "<VENV>")


def say(line: str) -> None:
    print(mask(line), flush=True)


def record(case: str, value) -> None:
    RESULTS["cases"][case] = json.loads(mask(json.dumps(value, default=str)))


def first_line(text: str) -> str:
    return str(text).strip().splitlines()[0] if str(text).strip() else ""


def admin_engine():
    import sqlalchemy as sa

    base = urlsplit(PGURL)
    return sa.create_engine(urlunsplit(base._replace(path="/postgres")), isolation_level="AUTOCOMMIT")


def new_db(name: str) -> str:
    import sqlalchemy as sa

    dbname = f"pai_{LABEL}_{name}".lower().replace(".", "_").replace("-", "_")
    engine = admin_engine()
    with engine.connect() as conn:
        conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)'))
        conn.execute(sa.text(f'CREATE DATABASE "{dbname}"'))
    engine.dispose()
    return urlunsplit(urlsplit(PGURL)._replace(path="/" + dbname))


def pg_facts(url: str) -> dict:
    import sqlalchemy as sa

    engine = sa.create_engine(url)
    with engine.connect() as conn:
        tables = conn.execute(
            sa.text("SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY 1")
        ).scalars().all()
        alembic = (
            conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalars().all()
            if "alembic_version" in tables
            else None
        )
        module = (
            conn.execute(sa.text("SELECT value FROM om_meta WHERE key='module_id'")).scalar()
            if "om_meta" in tables
            else None
        )
    engine.dispose()
    return {"table_count": len(tables), "tables": list(tables), "alembic_version": alembic, "om_meta_module_id": module}


def sqlite_master(path: Path) -> list:
    conn = sqlite3.connect(path)
    try:
        return [list(r) for r in conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name")]
    finally:
        conn.close()


def sqlite_facts(path: Path) -> dict:
    conn = sqlite3.connect(path)
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY 1")]
        alembic = (
            [r[0] for r in conn.execute("SELECT version_num FROM alembic_version")]
            if "alembic_version" in tables
            else None
        )
    finally:
        conn.close()
    return {"table_count": len(tables), "tables": tables, "alembic_version": alembic}


def cli(*args: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        [OM, "storage", "migrate", *args], capture_output=True, text=True, cwd=str(WORK)
    )
    return proc.returncode, proc.stdout, proc.stderr


def cli_step(case: str, args: list[str], *, text_too: bool = True) -> dict:
    code, out, err = cli(*args, "--json")
    try:
        entries = json.loads(out)["modules"]
    except Exception:  # noqa: BLE001
        entries = out.strip()[-600:]
    shown = ["openminion", "storage", "migrate", *args, "--json"]
    say(f"CASE {case}: $ {' '.join(shown)}")
    say(f"  exit={code} entries={json.dumps(entries)}")
    step = {"argv": [mask(a) for a in shown], "exit": code, "entries": entries, "stderr_tail": mask(err)[-1500:]}
    if text_too:
        tcode, tout, terr = cli(*args)
        say(f"  text mode: exit={tcode} output={tout.strip()!r}")
        step["text"] = {"exit": tcode, "stdout": mask(tout), "stderr_tail": mask(terr)[-800:]}
    return step


def in_process(module_id: str, url: str) -> str | None:
    """The CLI's own runner for one module (same helpers as `storage migrate`)."""
    from openminion.cli.commands import storage as storage_cli

    engine = storage_cli._get_postgres_engine(url)
    try:
        runner = storage_cli._make_runner(module_id, ":memory:", backend_type="postgres", engine=engine)
        report = runner.migrate(target="head")
        return "ok" if report.success else f"error: {report.error}"
    finally:
        engine.dispose()


def pkg_root() -> Path:
    import openminion

    return Path(openminion.__file__).resolve().parent


def storage_dir(module_id: str) -> Path:
    from openminion.modules.storage.migrations.alembic import discover_module_storage_root

    root = discover_module_storage_root(module_id)
    assert root is not None, module_id
    return root


def load_revision(path: Path):
    spec = importlib.util.spec_from_file_location(f"probe_rev_{path.parent.parent.parent.name}_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def case_ddl_scan() -> None:
    from openminion.modules.storage.migrations.registry import POSTGRES_VALIDATED_MODULES

    root = pkg_root()
    hits = []
    tuples = []
    for path in sorted(root.rglob("migrations/versions/*.py")):
        rel = str(path.relative_to(root.parent))
        declared = None
        try:
            declared = getattr(importlib.import_module(".".join(path.parent.parent.relative_to(root.parent).parts)), "MODULE_ID", None)
        except Exception:  # noqa: BLE001
            pass
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if "AUTOINCREMENT" in line.upper():
                hits.append({"file": rel, "line": number, "text": line.strip(), "module_id": declared})
        try:
            module = load_revision(path)
        except Exception as exc:  # noqa: BLE001
            tuples.append({"file": rel, "load_error": f"{type(exc).__name__}: {exc}"})
            continue
        ddl = getattr(module, "DDL", None)
        if ddl is None:
            continue
        auto = [first_line(s) for s in ddl if "AUTOINCREMENT" in str(s).upper()]
        if auto:
            tuples.append(
                {
                    "file": rel,
                    "module_id": declared,
                    "postgres_validated": declared in POSTGRES_VALIDATED_MODULES,
                    "has_POSTGRES_DDL": hasattr(module, "POSTGRES_DDL"),
                    "autoincrement_statements_in_DDL": auto,
                }
            )
    say(f"CASE ddl_scan: lines with AUTOINCREMENT in installed migrations/versions/*.py: {len(hits)}")
    for hit in hits:
        say(f"  {hit['file']}:{hit['line']} ({hit['module_id']}) {hit['text']}")
    say("  DDL tuples (including imported statements) with AUTOINCREMENT:")
    for row in tuples:
        say(f"  {json.dumps(row)}")
    record("ddl_scan", {"lines": hits, "ddl_tuples": tuples, "postgres_validated": sorted(POSTGRES_VALIDATED_MODULES)})


def case_pg_cli() -> None:
    out = {}
    for module_id in MODULES:
        url = new_db(module_id)
        step = cli_step(f"pg_cli {module_id}", ["--backend", "postgres", "--postgres-url", url, "--module", module_id])
        step["in_process"] = in_process(module_id, url)
        say(f"  in process (the CLI's _make_runner + migrate): {first_line(step['in_process'])}")
        step["after"] = pg_facts(url)
        say(f"  database after: {json.dumps(step['after'])}")
        out[module_id] = step
    record("pg_cli", out)


def case_pg_control() -> None:
    url = new_db("secret")
    step = cli_step("pg_control", ["--backend", "postgres", "--postgres-url", url, "--module", "secret"], text_too=False)
    step["after"] = pg_facts(url)
    say(f"  database after: {json.dumps(step['after'])}")
    record("pg_control", step)


def case_pg_each_module_alone() -> None:
    from openminion.cli.commands.storage import _get_validated_module_ids

    to_run, skipped = _get_validated_module_ids("postgres", None)
    say(f"CASE pg_each_module_alone: the CLI runs {to_run} and skips {skipped}")
    rows = {}
    for module_id in to_run:
        url = new_db(f"alone_{module_id}")
        code, out, _err = cli("--backend", "postgres", "--postgres-url", url, "--module", module_id, "--json")
        try:
            entry = json.loads(out)["modules"][0]
        except Exception:  # noqa: BLE001
            entry = {"raw": out.strip()[-300:]}
        message = entry.get("error")
        if entry.get("status") == "error" and not message:
            message = "(no message in the entry) in process: " + str(in_process(module_id, url))
        rows[module_id] = {"exit": code, "status": entry.get("status"), "error": message}
        say(f"  {module_id}: exit={code} status={entry.get('status')}" + (f" error={first_line(message)}" if message else ""))
    ok = [m for m, r in rows.items() if r["status"] == "ok"]
    say(f"  summary: ok={len(ok)} error={[m for m, r in rows.items() if r['status'] != 'ok']}")
    record("pg_each_module_alone", {"to_run": to_run, "skipped": skipped, "rows": rows})


def case_pg_full_run() -> None:
    url = new_db("full")
    step = cli_step("pg_full_run", ["--backend", "postgres", "--postgres-url", url], text_too=False)
    step["after"] = pg_facts(url)
    say(f"  database after: {json.dumps(step['after'])}")
    record("pg_full_run", step)


def run_statements(url: str, statements) -> list:
    import sqlalchemy as sa

    engine = sa.create_engine(url, isolation_level="AUTOCOMMIT")
    failures = []
    try:
        with engine.connect() as conn:
            for statement in statements:
                try:
                    conn.exec_driver_sql(str(statement).strip())
                except Exception as exc:  # noqa: BLE001
                    orig = getattr(exc, "orig", exc)
                    failures.append(
                        {"statement": first_line(statement), "error": f"{type(orig).__module__}.{type(orig).__name__}: {first_line(orig)}"}
                    )
    finally:
        engine.dispose()
    return failures


def case_pg_statements() -> None:
    out = {}
    for module_id in MODULES:
        baseline = load_revision(storage_dir(module_id) / "migrations" / "versions" / "0001_baseline.py")
        ddl = tuple(baseline.DDL)
        shipped = run_statements(new_db(f"stmt_{module_id}"), ddl)
        replaced = run_statements(new_db(f"stmt_serial_{module_id}"), tuple(str(s).replace(*SERIAL) for s in ddl))
        say(f"CASE pg_statements {module_id}: {len(ddl)} statements in 0001_baseline DDL, each run on its own")
        say(f"  shipped DDL: {len(shipped)} failed")
        for failure in shipped:
            say(f"    {failure['statement']} -> {failure['error']}")
        say(f"  with {SERIAL[0]!r} -> {SERIAL[1]!r}: {len(replaced)} failed {replaced}")
        out[module_id] = {"statements": len(ddl), "shipped_failures": shipped, "serial_failures": replaced}
    record("pg_statements", out)


def patched_copy(module_id: str) -> Path:
    target = WORK / f"patched-{module_id}"
    shutil.copytree(storage_dir(module_id) / "migrations", target / "migrations", ignore=shutil.ignore_patterns("__pycache__"))
    baseline = target / "migrations" / "versions" / "0001_baseline.py"
    with baseline.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(PATCH)
    return target / "migrations"


def runner_for(module_id: str, *, script_location: Path | None, db_path, engine=None):
    from openminion.modules.storage.migrations.module_ids import MODULE_APPLICATION_IDS
    from openminion.modules.storage.migrations.runner import MigrationRunner

    kwargs = dict(module_id=module_id, db_path=db_path, module_application_id=MODULE_APPLICATION_IDS[module_id])
    if script_location is not None:
        kwargs["alembic_script_location"] = script_location
    if engine is not None:
        kwargs.update(backend_type="postgres", engine=engine)
    return MigrationRunner(**kwargs)


def case_patched_copy() -> None:
    import sqlalchemy as sa

    out = {"patch": PATCH.strip()}
    engine0 = admin_engine()
    with engine0.connect() as conn:
        real = conn.execute(sa.text("SELECT CAST(CAST(1761650000.123456 AS REAL) AS DOUBLE PRECISION)")).scalar()
        double = conn.execute(sa.text("SELECT CAST(1761650000.123456 AS DOUBLE PRECISION)")).scalar()
    engine0.dispose()
    say(f"CASE patched_copy: on PostgreSQL 1761650000.123456 stored as REAL reads back {real!r}; as DOUBLE PRECISION {double!r}")
    out["real_vs_double"] = {"real": real, "double": double}
    for module_id in MODULES:
        location = patched_copy(module_id)
        url = new_db(f"patched_{module_id}")
        engine = sa.create_engine(url)
        try:
            report = runner_for(module_id, script_location=location, db_path=":memory:", engine=engine).migrate(target="head")
            checks = {}
            with engine.begin() as conn:
                if module_id == "telemetry":
                    conn.execute(
                        sa.text(
                            "INSERT INTO events (session_id, turn_id, event_type, timestamp, data) "
                            "VALUES ('s', 't', 'e', 1761650000.123456, '{}')"
                        )
                    )
                    checks["events.timestamp read back"] = conn.execute(sa.text("SELECT timestamp FROM events")).scalar()
                    table = "events"
                else:
                    for n in (1, 2):
                        conn.execute(
                            sa.text(
                                "INSERT INTO cp_inbound_messages (message_id, chat_key, user_key, timestamp, payload_json) "
                                f"VALUES ('m{n}', 'c', 'u', 't', '{{}}')"
                            )
                        )
                    checks["cp_inbound_messages ids"] = conn.execute(sa.text("SELECT id FROM cp_inbound_messages ORDER BY id")).scalars().all()
                    table = "cp_inbound_messages"
                checks["id column default"] = conn.execute(
                    sa.text(
                        "SELECT column_default FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name=:t AND column_name='id'"
                    ),
                    {"t": table},
                ).scalar()
        finally:
            engine.dispose()
        after = pg_facts(url)
        say(f"  {module_id} on PostgreSQL with the patched copy: success={report.success} error={first_line(report.error or '')}")
        say(f"    alembic_version={after['alembic_version']} tables={after['table_count']} checks={json.dumps(checks, default=str)}")

        shipped_db = WORK / f"sqlite-shipped-{module_id}" / f"{module_id}.db"
        patched_db = WORK / f"sqlite-patched-{module_id}" / f"{module_id}.db"
        for db in (shipped_db, patched_db):
            db.parent.mkdir(parents=True)
            sqlite3.connect(db).close()
        shipped_report = runner_for(module_id, script_location=None, db_path=shipped_db).migrate(target="head")
        patched_report = runner_for(module_id, script_location=location, db_path=patched_db).migrate(target="head")
        same_new = sqlite_master(shipped_db) == sqlite_master(patched_db)
        before = (sqlite_master(shipped_db), sqlite_facts(shipped_db)["alembic_version"])
        rerun = runner_for(module_id, script_location=location, db_path=shipped_db).migrate(target="head")
        after_rerun = (sqlite_master(shipped_db), sqlite_facts(shipped_db)["alembic_version"])
        say(
            f"    SQLite: shipped scripts success={shipped_report.success}, patched copy success={patched_report.success}; "
            f"sqlite_master identical={same_new}"
        )
        say(
            f"    SQLite, file migrated with the shipped scripts, then upgrade head with the patched copy: "
            f"success={rerun.success} alembic_version {before[1]} -> {after_rerun[1]}; sqlite_master unchanged={before[0] == after_rerun[0]}"
        )
        out[module_id] = {
            "postgres": {"success": report.success, "error": report.error, "after": after, "checks": checks},
            "sqlite": {
                "shipped_success": shipped_report.success,
                "patched_success": patched_report.success,
                "sqlite_master_identical": same_new,
                "rerun_success": rerun.success,
                "alembic_version_before_after": [before[1], after_rerun[1]],
                "sqlite_master_unchanged_after_rerun": before[0] == after_rerun[0],
            },
        }
    record("patched_copy", out)


def case_sqlite() -> None:
    out = {}
    for module_id in MODULES:
        db = WORK / f"sqlite-cli-{module_id}" / f"{module_id}.db"
        db.parent.mkdir(parents=True)
        sqlite3.connect(db).close()
        step = cli_step(f"sqlite {module_id}", ["--sqlite", str(db), "--module", module_id], text_too=False)
        step["after"] = sqlite_facts(db)
        say(f"  file after: {json.dumps(step['after'])}")
        out[module_id] = step
    record("sqlite", out)


def case_startup_check() -> None:
    from openminion.modules.storage.runtime.context import build_runtime_storage

    url = new_db("startup")
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, rec: logging.LogRecord) -> None:
            records.append(rec)

    handler = Capture(level=logging.WARNING)
    logger = logging.getLogger("openminion.modules.storage.runtime.validation")
    logger.addHandler(handler)
    try:
        ctx = build_runtime_storage(
            WORK / "state" / "openminion.db",
            env={},
            record_backend="record.postgres",
            record_backend_options={"url": url},
        )
        version = ctx.migration_result.current_version
        ctx.close()
        built = f"built, runtime schema v{version}"
    except Exception as exc:  # noqa: BLE001
        built = f"{type(exc).__name__}: {exc}"
    finally:
        logger.removeHandler(handler)
    warnings = sorted(rec.getMessage() for rec in records)
    say(f"CASE startup_check: build_runtime_storage(record_backend='record.postgres') on a new database: {built}")
    say(f"  validation warnings: {len(warnings)}")
    for message in warnings:
        if any(f"'{m}'" in message for m in MODULES):
            say(f"  warning: {message}")
    steps = {}
    for module_id in MODULES:
        step = cli_step(
            f"startup_check suggested command ({module_id})",
            ["--backend", "postgres", "--postgres-url", url, "--module", module_id],
            text_too=False,
        )
        steps[module_id] = step
    record("startup_check", {"build": built, "warnings": warnings, "suggested_commands": steps})


def main() -> int:
    import openminion
    import sqlalchemy as sa

    say(f"openminion {openminion.__version__} {openminion.__file__}")
    say(f"python {platform.python_version()} sqlite {sqlite3.sqlite_version} {platform.platform()}")
    RESULTS["openminion"] = {"version": openminion.__version__, "file": mask(openminion.__file__)}
    if not PGURL:
        say("PGURL is not set: PostgreSQL cases cannot run")
        return 2
    engine = admin_engine()
    with engine.connect() as conn:
        RESULTS["postgres"] = conn.execute(sa.text("select version()")).scalar()
    engine.dispose()
    say(f"postgres: {RESULTS['postgres']}")
    for case in (
        case_ddl_scan,
        case_pg_cli,
        case_pg_control,
        case_pg_each_module_alone,
        case_pg_full_run,
        case_pg_statements,
        case_patched_copy,
        case_sqlite,
        case_startup_check,
    ):
        try:
            case()
        except Exception:  # noqa: BLE001
            say(f"CASE {case.__name__} raised:\n{traceback.format_exc()}")
            RESULTS["cases"][case.__name__] = {"raised": mask(traceback.format_exc())}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"probe_postgres_autoincrement_baseline_{LABEL}.json").write_text(
        mask(json.dumps(RESULTS, indent=2, sort_keys=True, default=str)) + "\n", encoding="utf-8", newline="\n"
    )
    say("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
