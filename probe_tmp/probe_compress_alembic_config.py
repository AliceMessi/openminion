"""`compress` has no Alembic configuration for `openminion storage migrate`.

Usage: python -I -X utf8 probe_compress_alembic_config.py <out_dir> <label>

Needs PGURL (a PostgreSQL server URL, e.g. postgresql+psycopg://user:pw@host/postgres).
Every PostgreSQL case uses a new, empty database; every SQLite case a new file in
a new temporary directory. The CLI is the `openminion` console script next to
this interpreter. Passwords are masked in all output.

Cases:
- discovery: what `discover_module_storage_root` / `discover_module_alembic_paths`
  return for every module id, and every installed `*/storage/migrations/env.py`
  package checked against discovery for the module id it declares (MODULE_ID).
- pg_compress: `storage migrate --backend postgres --module compress` (json and
  text), then the same migration in process with the CLI's own helpers.
- pg_control: `--module secret`, the same way.
- pg_each_module_alone: each module the CLI runs on PostgreSQL, alone, on its own
  new database.
- pg_full_run: `storage migrate --backend postgres` (every module) on one database.
- pg_explicit_paths: MigrationRunner for compress with alembic_ini_path and
  alembic_script_location set to the installed compress storage package.
- sqlite: `--sqlite <file> --module compress`, `--module secret`, compress's own
  `run_migrations(db_path)`, and the explicit-path MigrationRunner on SQLite.
- startup_check: `build_runtime_storage` with the PostgreSQL record backend on a
  new database (warnings captured), then the command its warning suggests.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import platform
import re
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
WORK = Path(tempfile.mkdtemp(prefix="compress-alembic-"))
os.environ["OPENMINION_HOME"] = str(WORK / "home")
os.environ["OPENMINION_DATA_ROOT"] = str(WORK / "data")
OM = str(Path(sys.executable).parent / "openminion")
RESULTS: dict = {"label": LABEL, "cases": {}}


def mask(text: str) -> str:
    text = re.sub(r"(postgresql(\+[a-z0-9]+)?://[^:/@\s]+):[^@\s]*@", r"\1:***@", str(text))
    return text.replace(str(WORK), "<WORK>").replace(sys.prefix, "<VENV>")


def say(line: str) -> None:
    print(mask(line), flush=True)


def record(case: str, value) -> None:
    RESULTS["cases"][case] = value


def admin_engine():
    import sqlalchemy as sa

    base = urlsplit(PGURL)
    return sa.create_engine(urlunsplit(base._replace(path="/postgres")), isolation_level="AUTOCOMMIT")


def new_db(name: str) -> str:
    import sqlalchemy as sa

    dbname = f"cac_{LABEL}_{name}".lower().replace(".", "_").replace("-", "_")
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
    return {"tables": list(tables), "alembic_version": alembic, "om_meta_module_id": module}


def sqlite_facts(path: Path) -> dict:
    conn = sqlite3.connect(path)
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY 1")]
        alembic = (
            [r[0] for r in conn.execute("SELECT version_num FROM alembic_version")]
            if "alembic_version" in tables
            else None
        )
        module = (
            conn.execute("SELECT value FROM om_meta WHERE key='module_id'").fetchone()
            if "om_meta" in tables
            else None
        )
        app_id = conn.execute("PRAGMA application_id").fetchone()[0]
    finally:
        conn.close()
    return {
        "tables": tables,
        "alembic_version": alembic,
        "om_meta_module_id": module[0] if module else None,
        "application_id": hex(app_id),
    }


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
    return json.loads(mask(json.dumps(step)))


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


def case_discovery() -> None:
    import openminion
    from openminion.modules.storage.migrations import alembic as om_alembic
    from openminion.modules.storage.migrations.module_ids import MODULE_APPLICATION_IDS
    from openminion.modules.storage.migrations.registry import POSTGRES_VALIDATED_MODULES

    pkg_root = Path(openminion.__file__).resolve().parent
    by_module = {}
    for module_id in MODULE_APPLICATION_IDS:
        root = om_alembic.discover_module_storage_root(module_id)
        ini, script = om_alembic.discover_module_alembic_paths(module_id)
        by_module[module_id] = {
            "storage_root": str(root.relative_to(pkg_root.parent)) if root else None,
            "alembic_ini": bool(ini),
            "script_location": bool(script),
            "postgres_validated": module_id in POSTGRES_VALIDATED_MODULES,
        }
        say(
            f"CASE discovery module_id={module_id}: storage_root={by_module[module_id]['storage_root']} "
            f"alembic_ini={bool(ini)} script_location={bool(script)} "
            f"postgres_validated={module_id in POSTGRES_VALIDATED_MODULES}"
        )
    packages = []
    for env in sorted(pkg_root.rglob("migrations/env.py")):
        storage_dir = env.parent.parent
        dotted = ".".join(storage_dir.relative_to(pkg_root.parent).parts)
        try:
            declared = getattr(importlib.import_module(dotted + ".migrations"), "MODULE_ID", None)
        except Exception as exc:  # noqa: BLE001
            declared = f"<import failed: {exc}>"
        root = om_alembic.discover_module_storage_root(str(declared))
        found = root is not None and root.resolve() == storage_dir
        packages.append({"package": dotted, "module_id": declared, "discovered": found})
        say(f"CASE discovery package={dotted} MODULE_ID={declared!r} found_by_discovery={found}")
    mismatches = [p for p in packages if not p["discovered"]]
    say(f"CASE discovery packages_with_alembic={len(packages)} not_found_by_discovery={[p['module_id'] for p in mismatches]}")
    tries = {}
    for candidate in ("openminion.modules.compress.storage", "openminion.modules.context.compress.storage"):
        try:
            mod = importlib.import_module(candidate)
            tries[candidate] = "imports: " + str(Path(mod.__file__).resolve().parent.relative_to(pkg_root.parent))
        except Exception as exc:  # noqa: BLE001
            tries[candidate] = f"{type(exc).__name__}: {exc}"
        say(f"CASE discovery import {candidate}: {tries[candidate]}")
    record("discovery", {"modules": by_module, "packages": packages, "mismatches": mismatches, "imports": tries})


def case_pg_compress() -> None:
    url = new_db("compress")
    step = cli_step("pg_compress", ["--backend", "postgres", "--postgres-url", url, "--module", "compress"])
    step["in_process"] = in_process("compress", url)
    say(f"  in process (the CLI's _make_runner + migrate): {step['in_process']}")
    step["after"] = pg_facts(url)
    say(f"  database after: {json.dumps(step['after'])}")
    record("pg_compress", step)


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
        rows[module_id] = {"exit": code, "status": entry.get("status"), "error": mask(message) if message else None}
        say(f"  {module_id}: exit={code} status={entry.get('status')}" + (f" error={message}" if message else ""))
    ok = [m for m, r in rows.items() if r["status"] == "ok"]
    say(f"  summary: ok={len(ok)} error={[m for m, r in rows.items() if r['status'] != 'ok']}")
    record("pg_each_module_alone", {"to_run": to_run, "skipped": skipped, "rows": rows})


def case_pg_full_run() -> None:
    url = new_db("full")
    step = cli_step("pg_full_run", ["--backend", "postgres", "--postgres-url", url], text_too=True)
    step["after"] = pg_facts(url)
    say(f"  database after: {json.dumps(step['after'])}")
    record("pg_full_run", step)


def compress_storage_dir() -> Path:
    import openminion.modules.context.compress.storage as compress_storage

    return Path(compress_storage.__file__).resolve().parent


def explicit_ini(sdir: Path) -> Path | None:
    """alembic.ini when the installed package ships it (the wheel does not)."""
    ini = sdir / "alembic.ini"
    return ini if ini.exists() else None


def case_pg_explicit_paths() -> None:
    import sqlalchemy as sa
    from openminion.modules.storage.migrations.module_ids import MODULE_APPLICATION_IDS
    from openminion.modules.storage.migrations.runner import MigrationRunner

    url = new_db("explicit")
    sdir = compress_storage_dir()
    engine = sa.create_engine(url)
    try:
        runner = MigrationRunner(
            module_id="compress",
            db_path=":memory:",
            module_application_id=MODULE_APPLICATION_IDS["compress"],
            backend_type="postgres",
            engine=engine,
            alembic_ini_path=explicit_ini(sdir),
            alembic_script_location=sdir / "migrations",
        )
        report = runner.migrate(target="head")
        with engine.connect() as conn:
            identity = conn.execute(
                sa.text(
                    "SELECT is_identity FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='dropped_reasons' AND column_name='id'"
                )
            ).scalar()
    finally:
        engine.dispose()
    after = pg_facts(url)
    say(
        "CASE pg_explicit_paths: MigrationRunner(module_id='compress', backend_type='postgres', "
        f"alembic_ini_path={'<compress storage>/alembic.ini' if explicit_ini(sdir) else None}, "
        "alembic_script_location=<compress storage>/migrations)"
    )
    say(f"  success={report.success} error={report.error} dropped_reasons.id is_identity={identity}")
    say(f"  database after: {json.dumps(after)}")
    record("pg_explicit_paths", {"success": report.success, "error": report.error, "identity": identity, "after": after})


def case_sqlite() -> None:
    from openminion.modules.storage.migrations.module_ids import MODULE_APPLICATION_IDS
    from openminion.modules.storage.migrations.runner import MigrationRunner

    out = {}
    for module_id in ("compress", "secret"):
        db = WORK / f"sqlite-cli-{module_id}" / f"{module_id}.db"
        db.parent.mkdir(parents=True)
        sqlite3.connect(db).close()
        step = cli_step(f"sqlite_{module_id}", ["--sqlite", str(db), "--module", module_id], text_too=False)
        if step["entries"] and isinstance(step["entries"], list) and not step["entries"][0].get("error") and not step["entries"][0].get("success"):
            from openminion.cli.commands import storage as storage_cli

            report = storage_cli._make_runner(module_id, str(db)).migrate(target="head")
            step["in_process"] = "ok" if report.success else f"error: {report.error}"
            say(f"  in process (the CLI's _make_runner + migrate): {step['in_process']}")
        step["after"] = sqlite_facts(db)
        say(f"  file after: {json.dumps(step['after'])}")
        out[f"cli_{module_id}"] = step

    from openminion.modules.context.compress.storage.migrations import run_migrations

    db = WORK / "sqlite-own" / "compress.db"
    db.parent.mkdir(parents=True)
    try:
        run_migrations(db)
        own = "returned without error"
    except Exception as exc:  # noqa: BLE001
        own = f"{type(exc).__name__}: {exc}"
    say(f"CASE sqlite_own_call: openminion.modules.context.compress.storage.migrations.run_migrations(<new file>): {own}")
    out["own_call"] = {"result": own, "after": sqlite_facts(db)}
    say(f"  file after: {json.dumps(out['own_call']['after'])}")

    db = WORK / "sqlite-explicit" / "compress.db"
    db.parent.mkdir(parents=True)
    sqlite3.connect(db).close()
    sdir = compress_storage_dir()
    report = MigrationRunner(
        module_id="compress",
        db_path=db,
        module_application_id=MODULE_APPLICATION_IDS["compress"],
        alembic_ini_path=explicit_ini(sdir),
        alembic_script_location=sdir / "migrations",
    ).migrate(target="head")
    out["explicit_paths"] = {"success": report.success, "error": report.error, "after": sqlite_facts(db)}
    say(f"CASE sqlite_explicit_paths: success={report.success} error={report.error}")
    say(f"  file after: {json.dumps(out['explicit_paths']['after'])}")
    record("sqlite", json.loads(mask(json.dumps(out))))


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
    warnings = [rec.getMessage() for rec in records]
    say(f"CASE startup_check: build_runtime_storage(record_backend='record.postgres') on a new database: {built}")
    say(f"  validation warnings: {len(warnings)}")
    for message in sorted(warnings):
        if "'compress'" in message:
            say(f"  warning: {message}")
    step = cli_step(
        "startup_check suggested command",
        ["--backend", "postgres", "--postgres-url", url, "--module", "compress"],
        text_too=False,
    )
    record("startup_check", {"build": built, "warnings": sorted(warnings), "suggested_command": step})


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
        case_discovery,
        case_pg_compress,
        case_pg_control,
        case_pg_each_module_alone,
        case_pg_full_run,
        case_pg_explicit_paths,
        case_sqlite,
        case_startup_check,
    ):
        try:
            case()
        except Exception:  # noqa: BLE001
            say(f"CASE {case.__name__} raised:\n{traceback.format_exc()}")
            record(case.__name__, {"raised": mask(traceback.format_exc())})
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"probe_compress_alembic_config_{LABEL}.json").write_text(
        mask(json.dumps(RESULTS, indent=2, sort_keys=True, default=str)) + "\n", encoding="utf-8", newline="\n"
    )
    say("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
