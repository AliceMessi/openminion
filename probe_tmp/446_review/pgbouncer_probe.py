"""#446 review probe: what PgBouncer does with a module URL's `options`, and what the branch does.

Starts PgBouncer (the distribution's package) in front of the runner's PostgreSQL, in transaction
pooling, once with its defaults and once with `ignore_startup_parameters = options`. For each:
a plain connection with `options=-c search_path="..."` (what reaches the server), then the
branch's memory store and `storage migrate --module secret` through PgBouncer on a new database,
with a snapshot of the database's schemas and tables before and after.
Usage: python -I pgbouncer_probe.py   (OPENMINION_TEST_POSTGRES_URL: a superuser URL)
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import sqlalchemy as sa

WORK = Path(tempfile.mkdtemp(prefix="pgb-"))


def start(extra: str) -> subprocess.Popen:
    (WORK / "users.txt").write_text('"postgres" "test"\n', encoding="utf-8")
    ini = WORK / "pgbouncer.ini"
    ini.write_text(
        "[databases]\n* = host=127.0.0.1 port=5432\n\n[pgbouncer]\n"
        "listen_addr = 127.0.0.1\nlisten_port = 6433\nauth_type = plain\n"
        f"auth_file = {WORK / 'users.txt'}\npool_mode = transaction\n"
        f"unix_socket_dir =\n{extra}\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(["pgbouncer", str(ini)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(1.5)
    return proc


def stop(proc: subprocess.Popen) -> str:
    proc.terminate()
    out, _ = proc.communicate(timeout=10)
    lines = [l for l in out.splitlines() if "options" in l or "WARNING" in l or "ERROR" in l]
    return "\n".join(lines[-6:])


def snapshot(url: str) -> list:
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            return conn.execute(
                sa.text(
                    "SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n "
                    "ON n.oid = c.relnamespace WHERE n.nspname !~ '^pg_' AND "
                    "n.nspname <> 'information_schema' ORDER BY 1, 2"
                )
            ).all() + conn.execute(
                sa.text("SELECT nspname, '' FROM pg_namespace WHERE nspname !~ '^pg_' ORDER BY 1")
            ).all()
    finally:
        engine.dispose()


def case(label: str, extra: str, server: str) -> None:
    print(f"\n=== PgBouncer, transaction pooling, {label}")
    name = f"om446_pgb_{uuid.uuid4().hex[:8]}"
    admin = sa.create_engine(server, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    direct = sa.engine.make_url(server).set(database=name)
    pooled = direct.set(host="127.0.0.1", port=6433)
    direct_url = direct.render_as_string(hide_password=False)
    pooled_url = pooled.render_as_string(hide_password=False)
    proc = start(extra)
    try:
        options_url = pooled.update_query_dict({"options": '-c search_path="probe_schema"'})
        engine = sa.create_engine(options_url.render_as_string(hide_password=False))
        try:
            with engine.connect() as conn:
                print("plain connection with options: search_path =",
                      conn.execute(sa.text("SELECT current_setting('search_path')")).scalar())
        except Exception as exc:  # noqa: BLE001
            print("plain connection with options: error:", type(exc).__name__, str(exc).splitlines()[0][:200])
        finally:
            engine.dispose()

        before = snapshot(direct_url)
        from openminion.modules.memory.storage.factory import resolve_memory_backend

        try:
            store = resolve_memory_backend(
                config={"store": {"backend": "postgres", "postgres_url": pooled_url}},
                db_path=WORK / "memory.db",
            ).store
            print("memory store through PgBouncer: opened")
            store.close()
        except Exception as exc:  # noqa: BLE001
            print("memory store through PgBouncer:", type(exc).__name__, str(exc).splitlines()[0][:400])
        print("database unchanged after the memory store attempt:", snapshot(direct_url) == before)
        from openminion.cli.commands.storage import run_storage_migrate
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                run_storage_migrate(SimpleNamespace(backend="postgres", postgres_url=pooled_url, sqlite=str(WORK / "u.db"), module="secret", plan=False, verify=False, json=True))
            except SystemExit:
                pass
        try:
            entry = json.loads(buf.getvalue())["modules"][0]
            print("storage migrate --module secret through PgBouncer:", entry["status"], str(entry.get("error") or "")[:300])
        except Exception:  # noqa: BLE001
            print("storage migrate output:", buf.getvalue()[:300])
        after = snapshot(direct_url)
        print("schemas after:", sorted({s for s, _ in after}))
        print("memory_records in:", sorted(s for s, t in after if t == "memory_records"))
        # One pooled server connection, two clients: does one client's search_path leak?
        a_url = pooled.update_query_dict({"options": '-c search_path="public__memory"'}).render_as_string(hide_password=False)
        try:
            ea, eb = sa.create_engine(a_url), sa.create_engine(pooled_url)
            with ea.connect() as ca, eb.connect() as cb:
                for _ in range(3):
                    sa_path = ca.execute(sa.text("SHOW search_path")).scalar()
                    ca.commit()
                    sb_path = cb.execute(sa.text("SHOW search_path")).scalar()
                    cb.commit()
                print("two clients, search_path:", sa_path, "|", sb_path)
            ea.dispose(); eb.dispose()
        except Exception as exc:  # noqa: BLE001
            print("two clients: error:", str(exc).splitlines()[0][:200])
    finally:
        print("pgbouncer log:", stop(proc))
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


def main() -> None:
    server = os.environ["OPENMINION_TEST_POSTGRES_URL"]
    print(subprocess.run(["pgbouncer", "--version"], capture_output=True, text=True).stdout.strip())
    case("defaults", "", server)
    case("ignore_startup_parameters = options", "ignore_startup_parameters = options", server)
    case("track_extra_parameters = search_path", "track_extra_parameters = search_path", server)


if __name__ == "__main__":
    main()
