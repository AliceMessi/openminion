"""#446 review probe: what the session store leaves next to runtime storage on dev.

Runtime storage starts on a new database (`public`), then the session module store is
built on the same URL the way `api/queries/sessions.py` builds it on dev. Prints the
store's error and every object it left in `public` (tables, columns, indexes, sequences).
Usage: python -I leftover.py   (OPENMINION_TEST_POSTGRES_URL: a superuser URL)
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path

import sqlalchemy as sa


def objects(url: str) -> dict:
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            rel = conn.execute(
                sa.text(
                    "SELECT c.relname, c.relkind FROM pg_class c JOIN pg_namespace n "
                    "ON n.oid = c.relnamespace WHERE n.nspname = 'public' ORDER BY 1"
                )
            ).all()
            cols = conn.execute(
                sa.text(
                    "SELECT table_name, column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = 'public' ORDER BY 1, 2"
                )
            ).all()
            schemas = conn.execute(
                sa.text(
                    "SELECT nspname FROM pg_namespace WHERE nspname !~ '^pg_' "
                    "AND nspname <> 'information_schema' ORDER BY 1"
                )
            ).scalars().all()
    finally:
        engine.dispose()
    return {
        "relations": sorted(f"{name} ({kind})" for name, kind in rel),
        "columns": sorted(f"{t}.{c} {d}" for t, c, d in cols),
        "schemas": list(schemas),
    }


def main() -> None:
    server = os.environ["OPENMINION_TEST_POSTGRES_URL"]
    name = f"om446_left_{uuid.uuid4().hex[:8]}"
    admin = sa.create_engine(server, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    url = sa.engine.make_url(server).set(database=name).render_as_string(hide_password=False)
    root = Path(tempfile.mkdtemp())

    from openminion.modules.session.runtime.factory import build_module_session_store
    from openminion.modules.storage.engine import StorageEngineConfig
    from openminion.modules.storage.runtime.context import build_runtime_storage

    ctx = build_runtime_storage(
        root / "state.db", env={}, record_backend="record.postgres",
        record_backend_options={"url": url},
    )
    print("runtime version", ctx.migration_result.current_version)
    ctx.close()
    before = objects(url)

    config = StorageEngineConfig(
        root_dir=root / "s", sqlite_path=root / "s" / "sessions.db", fallback_root=root / "s",
        record_backend="record.postgres", record_backend_options={"url": url},
    )
    try:
        store = build_module_session_store(
            config=config, database_path=root / "s" / "sessions.db", env={}
        )
        print("session store: opened")
        store.close()
    except Exception as exc:  # noqa: BLE001
        print("session store error:", type(exc).__name__, str(exc).splitlines()[0][:300])
    after = objects(url)

    print(json.dumps({
        "added_relations": sorted(set(after["relations"]) - set(before["relations"])),
        "removed_relations": sorted(set(before["relations"]) - set(after["relations"])),
        "added_columns": sorted(set(after["columns"]) - set(before["columns"])),
        "schemas": after["schemas"],
    }, indent=1))
    with admin.connect() as conn:
        conn.execute(sa.text(f'DROP DATABASE "{name}" WITH (FORCE)'))
    admin.dispose()


if __name__ == "__main__":
    main()
