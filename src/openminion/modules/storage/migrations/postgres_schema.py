"""PostgreSQL schema selection for runtime storage and module stores (#446).

Runtime storage and every module used to write into the one schema the configured URL selects, so
they shared ``alembic_version`` and ``om_meta`` and adopted each other's tables through
``CREATE TABLE IF NOT EXISTS``. Now:

- runtime storage keeps that base schema, ``current_schema()`` of the configured URL;
- each module uses its own schema, ``<base>__<module id>``;
- a module whose history is already in the base schema keeps using it, with no move and no stamp.

An owner's history in a schema is proven only by its ledger together with an exact match of one of
the histories listed in ``postgres_histories.json``: the same tables, each with the same columns,
types, NOT NULL and primary key. The ledger is the ``migrations`` rows v1..vk for runtime storage, and
for a module ``om_meta.module_id`` with its ``alembic_version`` row, or with ``om_meta.schema_head``
for a store that has no Alembic ledger. The listed histories were measured from published packages
and from this code. Anything unproven, or a table that two recorded histories include, is refused
before any write; the message names the tables and the listed histories that include them.

The checks read only catalogs and ledger rows, with explicit schema names, in one READ ONLY
transaction.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openminion.modules.storage.constants import POSTGRES_MAX_IDENTIFIER_BYTES
from openminion.modules.storage.migrations.errors import SchemaOwnershipError

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, Engine

    from openminion.modules.storage.engine import StorageEngineConfig

RUNTIME = "runtime"
_HISTORIES_FILE = Path(__file__).with_name("postgres_histories.json")
_LEDGER_TABLES = frozenset({"alembic_version", "om_meta"})
_SAFE_NAME = re.compile(r"[A-Za-z0-9_]+")
_NOTHING_WRITTEN = (
    "Nothing was written. OpenMinion does not move, repair or restamp existing tables."
)
_GUIDANCE = (
    "Stop every OpenMinion process, take a full pg_dump, and follow "
    '"PostgreSQL schemas per owner" in docs/runtime-database-upgrades.md.'
)


@dataclass(frozen=True)
class History:
    """One supported layout of an owner: its ledger and its exact tables."""

    owner: str
    ledger: str  # "v3", "alembic 0004_namespace" or "store 0001_baseline"
    tables: dict[str, str]  # table -> signature
    measured: tuple[str, ...]

    @property
    def label(self) -> str:
        name = "runtime storage" if self.owner == RUNTIME else self.owner
        return f"{name} {self.ledger}"


@dataclass(frozen=True)
class _Catalog:
    engine_tables: frozenset[str]
    runtime_versions: dict[int, str]
    histories: dict[str, dict[str, tuple[History, ...]]]  # owner -> ledger -> histories
    names: dict[str, frozenset[str]]  # owner -> every table name in its histories
    signatures: dict[str, frozenset[str]]  # "owner/table" -> every listed signature


@lru_cache(maxsize=1)
def _catalog() -> _Catalog:
    data = json.loads(_HISTORIES_FILE.read_text(encoding="utf-8"))
    signatures: dict[str, str] = data["signatures"]
    by_owner: dict[str, dict[str, list[History]]] = {}
    for item in data["histories"]:
        history = History(
            owner=item["owner"],
            ledger=item["ledger"],
            tables={ref.split("/")[0]: signatures[ref] for ref in item["tables"]},
            measured=tuple(item["measured"]),
        )
        ledgers_of = by_owner.setdefault(history.owner, {})
        ledgers_of.setdefault(history.ledger, []).append(history)
    histories = {
        owner: {ledger: tuple(items) for ledger, items in ledgers.items()}
        for owner, ledgers in by_owner.items()
    }
    names: dict[str, frozenset[str]] = {}
    sigs: dict[str, set[str]] = {}
    for owner, ledgers in histories.items():
        names[owner] = frozenset(
            table for items in ledgers.values() for h in items for table in h.tables
        )
        for items in ledgers.values():
            for h in items:
                for table, signature in h.tables.items():
                    sigs.setdefault(f"{owner}/{table}", set()).add(signature)
    return _Catalog(
        engine_tables=frozenset(data["engine_tables"]),
        runtime_versions={int(k): v for k, v in data["runtime_versions"].items()},
        histories=histories,
        names=names,
        signatures={key: frozenset(value) for key, value in sigs.items()},
    )


def supported_owners() -> tuple[str, ...]:
    return tuple(sorted(_catalog().histories))


@dataclass(frozen=True)
class SchemaLayout:
    """What one schema holds, as far as ownership is concerned."""

    name: str
    exists: bool
    tables: dict[str, str | None] = field(
        default_factory=dict
    )  # signature for known names
    alembic: tuple[str, ...] | None = None
    om_meta: dict[str, str] | None = None
    migrations: tuple[tuple[int, str], ...] | None = None
    objects: int = 0
    owner: str | None = None
    usage: bool = False
    create: bool = False

    def ledger(self, owner: str) -> str | None:
        """The ledger key this schema records for ``owner``, or None."""
        if owner == RUNTIME:
            rows = self.migrations or ()
            versions = _catalog().runtime_versions
            if rows and list(rows) == [
                (v, versions.get(v)) for v in range(1, len(rows) + 1)
            ]:
                return f"v{len(rows)}"
            return None
        meta = self.om_meta or {}
        if meta.get("module_id") != owner:
            return None
        if self.alembic is None:
            return f"store {meta.get('schema_head', '')}"
        if len(self.alembic) == 1:
            return f"alembic {self.alembic[0]}"
        return None


@dataclass(frozen=True)
class ModuleSchema:
    """Where a module keeps its PostgreSQL tables."""

    module_id: str
    base: str
    schema: str
    create: bool = False

    @property
    def in_place(self) -> bool:
        return self.schema == self.base

    def url(self, configured_url: str) -> str:
        """The URL a module connection uses: unchanged in place, else its schema appended."""
        if self.in_place:
            return configured_url
        return module_url(configured_url, self.schema)


# ---------------------------------------------------------------------------- catalog reads


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def read_schema(connection: Connection, schema: str) -> SchemaLayout:
    """Read one schema's tables, signatures and ledger rows (explicit schema names only)."""
    from sqlalchemy import text

    row = connection.execute(
        text(
            "SELECT oid, pg_get_userbyid(nspowner), has_schema_privilege(oid, 'USAGE'), "
            "has_schema_privilege(oid, 'CREATE') FROM pg_catalog.pg_namespace "
            "WHERE nspname = :s"
        ),
        {"s": schema},
    ).first()
    if row is None:
        return SchemaLayout(name=schema, exists=False)
    oid, owner, usage, create = row
    known = {n for names in _catalog().names.values() for n in names} | _LEDGER_TABLES
    names = [
        r[0]
        for r in connection.execute(
            text(
                "SELECT relname FROM pg_catalog.pg_class WHERE relnamespace = :o "
                "AND relkind IN ('r', 'p')"
            ),
            {"o": oid},
        )
    ]
    wanted = [n for n in names if n in known]
    columns: dict[str, list[tuple[str, str, bool]]] = {}
    for rel, att, typ, notnull in connection.execute(
        text(
            "SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), "
            "a.attnotnull FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid "
            "WHERE c.relnamespace = :o AND c.relname = ANY(:n) "
            "AND a.attnum > 0 AND NOT a.attisdropped"
        ),
        {"o": oid, "n": wanted},
    ):
        columns.setdefault(str(rel), []).append((str(att), str(typ), bool(notnull)))
    keys: dict[str, list[str]] = {}
    for rel, att in connection.execute(
        text(
            "SELECT c.relname, a.attname FROM pg_catalog.pg_index i "
            "JOIN pg_catalog.pg_class c ON c.oid = i.indrelid "
            "JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid "
            "AND a.attnum = ANY(i.indkey) "
            "WHERE i.indisprimary AND c.relnamespace = :o AND c.relname = ANY(:n)"
        ),
        {"o": oid, "n": wanted},
    ):
        keys.setdefault(str(rel), []).append(str(att))
    tables: dict[str, str | None] = {
        n: (signature(columns.get(n, []), keys.get(n, [])) if n in known else None)
        for n in names
    }
    objects: int = connection.execute(
        text(
            "SELECT (SELECT count(*) FROM pg_catalog.pg_class WHERE relnamespace = :o) "
            "+ (SELECT count(*) FROM pg_catalog.pg_proc WHERE pronamespace = :o) "
            "+ (SELECT count(*) FROM pg_catalog.pg_type WHERE typnamespace = :o "
            "AND typrelid = 0 AND typelem = 0)"
        ),
        {"o": oid},
    ).scalar_one()
    return SchemaLayout(
        name=schema,
        exists=True,
        tables=tables,
        alembic=_ledger_rows(connection, schema, columns, "alembic_version"),
        om_meta=_om_meta(connection, schema, columns),
        migrations=_runtime_rows(connection, schema, columns),
        objects=int(objects),
        owner=str(owner),
        usage=bool(usage),
        create=bool(create),
    )


def signature(columns: list[tuple[str, str, bool]], primary_key: list[str]) -> str:
    """``name type [NOT NULL], ...; pk(a, b)`` with columns in name order."""
    cols = ", ".join(
        f"{name} {typ}{' NOT NULL' if notnull else ''}"
        for name, typ, notnull in sorted(columns)
    )
    return f"{cols}; pk({', '.join(sorted(primary_key))})"


def _has(
    columns: dict[str, list[tuple[str, str, bool]]], table: str, *names: str
) -> bool:
    return set(names) <= {c[0] for c in columns.get(table, [])}


def _ledger_rows(
    connection: Connection,
    schema: str,
    columns: dict[str, list[tuple[str, str, bool]]],
    table: str,
) -> tuple[str, ...] | None:
    from sqlalchemy import text

    if table not in columns:
        return None
    if not _has(columns, table, "version_num"):
        return ("<alembic_version without version_num>",)
    rows = connection.execute(
        text(f"SELECT version_num FROM {_quote(schema)}.alembic_version")
    )
    return tuple(sorted(str(r[0]) for r in rows))


def _om_meta(
    connection: Connection,
    schema: str,
    columns: dict[str, list[tuple[str, str, bool]]],
) -> dict[str, str] | None:
    from sqlalchemy import text

    if "om_meta" not in columns:
        return None
    if not _has(columns, "om_meta", "key", "value"):
        return {"module_id": "<om_meta without key/value>"}
    rows = connection.execute(
        text(
            f"SELECT key, value FROM {_quote(schema)}.om_meta "
            "WHERE key IN ('module_id', 'schema_head')"
        )
    )
    return {str(r[0]): str(r[1]) for r in rows}


def _runtime_rows(
    connection: Connection,
    schema: str,
    columns: dict[str, list[tuple[str, str, bool]]],
) -> tuple[tuple[int, str], ...] | None:
    from sqlalchemy import text

    if "migrations" not in columns or not _has(
        columns, "migrations", "version", "name"
    ):
        return None
    rows = connection.execute(
        text(f"SELECT version, name FROM {_quote(schema)}.migrations ORDER BY version")
    )
    return tuple((int(r[0]), str(r[1])) for r in rows)


# ---------------------------------------------------------------------------- histories


def complete_history(owner: str, layout: SchemaLayout) -> History | None:
    """The listed history the schema's ledger records for ``owner``, if all its tables match."""
    ledger = layout.ledger(owner)
    if ledger is None:
        return None
    matches = [
        history
        for history in _catalog().histories.get(owner, {}).get(ledger, ())
        if all(layout.tables.get(t) == sig for t, sig in history.tables.items())
    ]
    if not matches:
        return None
    # A store's extra tables make a second listed history with the same ledger: prefer the
    # one that holds exactly the owner's tables here, else the largest.
    owned = _owned_tables(owner, layout)
    exact = [history for history in matches if set(history.tables) == owned]
    return (exact or sorted(matches, key=lambda h: -len(h.tables)))[0]


def _owned_tables(owner: str, layout: SchemaLayout) -> set[str]:
    return {t for t in layout.tables if t in _catalog().names.get(owner, frozenset())}


def _claims(layout: SchemaLayout) -> dict[str, list[History]]:
    """Table -> the complete histories that include it, over every listed owner."""
    claims: dict[str, list[History]] = {}
    for owner in _catalog().histories:
        history = complete_history(owner, layout)
        if history is not None:
            for table in history.tables:
                claims.setdefault(table, []).append(history)
    return claims


def _matching_histories(table: str, sig: str | None) -> str:
    """The listed histories that include ``table`` with exactly ``sig``, as ranges."""
    groups: dict[tuple[str, str], list[str]] = {}
    for owner, ledgers in sorted(_catalog().histories.items()):
        for ledger, items in ledgers.items():
            if any(h.tables.get(table) == sig for h in items):
                kind, _, revision = ledger.rpartition(" ")
                key = (items[0].label.rsplit(" ", 1)[0], kind)
                if revision not in groups.setdefault(key, []):
                    groups[key].append(revision)
    return ", ".join(
        f"{name} {revisions[0]}" + (f"..{revisions[-1]}" if len(revisions) > 1 else "")
        for (name, _kind), revisions in groups.items()
    )


def _describe(
    table: str, layout: SchemaLayout, claims: dict[str, list[History]]
) -> str:
    holders = claims.get(table, [])
    if holders:
        return f"{table} (part of {' and '.join(h.label for h in holders)})"
    matches = _matching_histories(table, layout.tables.get(table))
    if not matches:
        return f"{table} (matches no listed history)"
    return f"{table} (its columns match {matches}; no ledger here records that history)"


def _proven_in_place(
    owner: str, layout: SchemaLayout, claims: dict[str, list[History]]
) -> History | None:
    history = complete_history(owner, layout)
    if history is None or _owned_tables(owner, layout) != set(history.tables):
        return None
    if any(claims.get(t, []) != [history] for t in history.tables):
        return None
    return history


def _unproven(
    owner: str, layout: SchemaLayout, claims: dict[str, list[History]]
) -> str:
    """Why ``owner``'s objects in ``layout`` are not a proven history, for a refusal."""
    name = "runtime storage" if owner == RUNTIME else owner
    owned = _owned_tables(owner, layout)
    ledger = layout.ledger(owner)
    listed = _catalog().histories.get(owner, {}).get(ledger or "", ())
    history = complete_history(owner, layout)
    reasons: list[str] = []
    explained: set[str] = set()
    if ledger is None:
        reasons.append(f"no {name} ledger is recorded there")
    elif not listed:
        reasons.append(
            f"its ledger records {ledger}, which is not a listed {name} history"
        )
    elif history is None:
        closest = max(
            listed,
            key=lambda h: sum(layout.tables.get(t) == g for t, g in h.tables.items()),
        )
        missing = sorted(t for t in closest.tables if t not in layout.tables)
        differ = sorted(
            t
            for t in closest.tables
            if t in layout.tables
            and t not in missing
            and layout.tables[t] != closest.tables[t]
        )
        parts = [f"{t} has other columns" for t in differ] + [
            f"{t} is missing" for t in missing
        ]
        reasons.append(f"its ledger records {closest.label}, but " + ", ".join(parts))
        explained = set(closest.tables)
    for table in sorted(owned - explained):
        if (
            history is not None
            and table in history.tables
            and claims.get(table) == [history]
        ):
            continue
        reasons.append(_describe(table, layout, claims))
    return "; ".join(reasons)


# ---------------------------------------------------------------------------- decisions


def runtime_refusal(layout: SchemaLayout) -> str | None:
    """None when runtime storage can use the base schema, else the refusal message."""
    if not _owned_tables(RUNTIME, layout):
        return None
    claims = _claims(layout)
    if _proven_in_place(RUNTIME, layout, claims) is not None:
        return None
    return (
        f"Runtime storage will not use PostgreSQL schema {_quote(layout.name)}: tables it "
        f"creates or reads there are not provably its own: "
        f"{_unproven(RUNTIME, layout, claims)}. {_NOTHING_WRITTEN} {_GUIDANCE}"
    )


def check_runtime_schema(engine: Engine) -> None:
    """Refuse, before any write, a base schema runtime storage cannot prove is its own."""
    with _read_only(engine) as connection:
        base = _current_schema(connection)
        if base is None:
            raise SchemaOwnershipError(
                "Runtime storage: the PostgreSQL search_path names no existing schema. "
                f"{_NOTHING_WRITTEN}"
            )
        message = runtime_refusal(read_schema(connection, base))
    if message is not None:
        raise SchemaOwnershipError(message)


def check_runtime_postgres_url(url: str) -> None:
    """:func:`check_runtime_schema` for a configured URL, on a short-lived connection."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    probe = create_engine(url, poolclass=NullPool)
    try:
        check_runtime_schema(probe)
    finally:
        probe.dispose()


def derived_schema_name(base: str, module_id: str) -> str:
    return f"{base}__{module_id}"


def _name_problem(name: str) -> str | None:
    if not _SAFE_NAME.fullmatch(name):
        return "it may only contain letters, digits and underscores"
    if len(name.encode("utf-8")) > POSTGRES_MAX_IDENTIFIER_BYTES:
        return f"it is longer than {POSTGRES_MAX_IDENTIFIER_BYTES} bytes"
    return None


def decide_module_schema(
    module_id: str,
    base: SchemaLayout,
    derived: SchemaLayout,
    *,
    role: str,
    database_create: bool,
) -> ModuleSchema:
    """Choose the module's schema from the configured base schema; raise when unproven."""
    claims = _claims(base)
    owned = _owned_tables(module_id, base)
    disputed = sorted(
        t
        for t in owned
        if len(claims.get(t, [])) != 1 or claims[t][0].owner == module_id
    )
    if (base.om_meta or {}).get("module_id") == module_id or disputed:
        if derived.exists:
            raise SchemaOwnershipError(
                f"Module {module_id!r} has objects in both {_quote(base.name)} "
                f"({', '.join(disputed) or 'its om_meta ledger'}) and {_quote(derived.name)}; "
                "neither is selected. Keep one location, and move the other to a schema "
                f"that is not named <base>__<module id>. {_NOTHING_WRITTEN} {_GUIDANCE}"
            )
        if _proven_in_place(module_id, base, claims) is not None:
            return ModuleSchema(module_id, base.name, base.name)
        raise SchemaOwnershipError(
            f"Module {module_id!r} will not use PostgreSQL schema {_quote(base.name)}: "
            f"its objects there are not provably its own: "
            f"{_unproven(module_id, base, claims)}. {_NOTHING_WRITTEN} {_GUIDANCE}"
        )
    problem = _name_problem(derived.name)
    if problem is not None:
        raise SchemaOwnershipError(
            f"Module {module_id!r} needs its own PostgreSQL schema {derived.name!r}, but "
            f"{problem}. Use a base schema named with letters, digits and underscores, "
            f"at most {POSTGRES_MAX_IDENTIFIER_BYTES - 2 - len(module_id)} bytes. {_NOTHING_WRITTEN}"
        )
    if not derived.exists:
        if not database_create:
            raise SchemaOwnershipError(
                f"Module {module_id!r} needs to create PostgreSQL schema "
                f"{_quote(derived.name)}, and role {role!r} has no CREATE privilege on the "
                "database. A database administrator can run "
                f"CREATE SCHEMA {_quote(derived.name)} AUTHORIZATION {_quote(role)}; "
                f"or GRANT CREATE ON DATABASE <database> TO {_quote(role)}; "
                f"{_NOTHING_WRITTEN}"
            )
        return ModuleSchema(module_id, base.name, derived.name, create=True)
    refusal = derived_refusal(module_id, derived, role=role)
    if refusal is not None:
        raise SchemaOwnershipError(refusal)
    return ModuleSchema(module_id, base.name, derived.name)


def derived_refusal(module_id: str, layout: SchemaLayout, *, role: str) -> str | None:
    """None when an existing schema holds nothing but ``module_id``'s own objects.

    That is: nothing at all (a schema a database administrator created); the module's proven
    history; or, with no ledger yet (a store bootstrap that stopped before writing ``om_meta``),
    only tables whose names and exact columns are listed for the module. The storage engine's
    own tables are allowed in every case.
    """
    if not (layout.usage and layout.create):
        return (
            f"Module {module_id!r}: role {role!r} lacks USAGE or CREATE on PostgreSQL "
            f"schema {_quote(layout.name)} (owner {layout.owner}). A database administrator "
            f"can run GRANT USAGE, CREATE ON SCHEMA {_quote(layout.name)} TO {_quote(role)}; "
            f"{_NOTHING_WRITTEN}"
        )
    catalog = _catalog()
    where = f"Module {module_id!r}: PostgreSQL schema {_quote(layout.name)}"
    if not layout.tables:
        if layout.objects:
            return f"{where} holds objects but no table; it is not selected. {_NOTHING_WRITTEN}"
        return None
    tables = {
        t: sig
        for t, sig in layout.tables.items()
        if t not in catalog.engine_tables and t not in _LEDGER_TABLES
    }
    foreign = sorted(t for t in tables if t not in catalog.names.get(module_id, ()))
    if foreign:
        return (
            f"{where} holds tables that are not {module_id}'s: {', '.join(foreign)}. "
            f"{_NOTHING_WRITTEN}"
        )
    meta = (layout.om_meta or {}).get("module_id")
    if meta not in (None, module_id) or layout.migrations:
        recorded = (
            f"om_meta names {meta!r}"
            if meta not in (None, module_id)
            else ("migrations holds runtime storage ledger rows")
        )
        return f"{where} records another owner's ledger: {recorded}. {_NOTHING_WRITTEN}"
    if not tables and not layout.alembic:
        # Only the module's own ledger tables, or the storage engine's: a store that
        # stopped before its first table, or a module downgraded to base.
        return None
    if layout.alembic or meta == module_id:
        claims = _claims(layout)
        if _proven_in_place(module_id, layout, claims) is not None:
            return None
        return (
            f"{where} is not provably {module_id}'s: "
            f"{_unproven(module_id, layout, claims)}. {_NOTHING_WRITTEN}"
        )
    wrong = sorted(
        t
        for t, sig in tables.items()
        if sig not in catalog.signatures.get(f"{module_id}/{t}", frozenset())
    )
    if wrong:
        return (
            f"{where} holds tables whose columns match no listed {module_id} history: "
            f"{', '.join(wrong)}. {_NOTHING_WRITTEN}"
        )
    return None


def select_module_schema(engine: Engine, module_id: str) -> ModuleSchema:
    """Choose the schema of ``module_id`` under the configured URL's base schema.

    Read only; raises :class:`SchemaOwnershipError` before any write when the choice is not
    proven.
    """
    with _read_only(engine) as connection:
        base_name = _current_schema(connection)
        if base_name is None:
            raise SchemaOwnershipError(
                f"Module {module_id!r}: the PostgreSQL search_path names no existing schema. "
                f"{_NOTHING_WRITTEN}"
            )
        role, database_create = connection.execute(
            _sql(
                "SELECT current_user, "
                "has_database_privilege(current_database(), 'CREATE')"
            )
        ).one()
        base = read_schema(connection, base_name)
        derived_name = derived_schema_name(base_name, module_id)
        derived = (
            read_schema(connection, derived_name)
            if _name_problem(derived_name) is None
            else SchemaLayout(name=derived_name, exists=False)
        )
    return decide_module_schema(
        module_id,
        base,
        derived,
        role=str(role),
        database_create=bool(database_create),
    )


def check_module_connection(engine: Engine, module_id: str) -> ModuleSchema:
    """For an engine handed to a module directly: its current schema must be the module's own.

    The schema is used in place when it holds the module's proven history, or as the module's
    own schema when it holds nothing but the module's objects. Read only.
    """
    with _read_only(engine) as connection:
        name = _current_schema(connection)
        if name is None:
            raise SchemaOwnershipError(
                f"Module {module_id!r}: the PostgreSQL search_path names no existing schema. "
                f"{_NOTHING_WRITTEN}"
            )
        role = str(connection.execute(_sql("SELECT current_user")).scalar_one())
        layout = read_schema(connection, name)
    claims = _claims(layout)
    if _proven_in_place(module_id, layout, claims) is not None:
        return ModuleSchema(module_id, name, name)
    refusal = derived_refusal(module_id, layout, role=role)
    if refusal is None:
        return ModuleSchema(module_id, name, name)
    raise SchemaOwnershipError(refusal)


# ---------------------------------------------------------------------------- connections


def module_url(url: str, schema: str) -> str:
    """``url`` with ``-c search_path="<schema>"`` appended to its ``options``.

    Every other query parameter and options token is kept; when the URL has no ``options``,
    ``PGOPTIONS`` is the starting value, because URL options replace it.
    """
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    current = parsed.query.get("options")
    if isinstance(current, tuple):
        raise SchemaOwnershipError(
            "The PostgreSQL URL repeats the options parameter; give one options value."
        )
    if current is None:
        from openminion.base.config.env import resolve_environment_config

        current = str(resolve_environment_config().get("PGOPTIONS", "") or "")
    existing = current
    options = f'{existing} -c search_path="{schema}"'.strip()
    return parsed.update_query_dict({"options": options}).render_as_string(
        hide_password=False
    )


def require_connection_schema(engine: Engine, schema: str) -> None:
    """Fail closed when a connection does not land in ``schema`` (a proxy that drops options)."""
    with engine.connect() as connection:
        current = _current_schema(connection)
    if current != schema:
        raise SchemaOwnershipError(
            f"A module connection selected schema {current!r} instead of {schema!r}; the "
            "server or a proxy did not apply the options parameter. "
            f"{_NOTHING_WRITTEN}"
        )


def create_module_schema(engine: Engine, selection: ModuleSchema) -> None:
    """Create a selected derived schema in its own transaction (store bootstraps)."""
    if not selection.create:
        return
    with engine.begin() as connection:
        connection.execute(_sql(f"CREATE SCHEMA {_quote(selection.schema)}"))


def enter_module_schema(connection: Connection, selection: ModuleSchema) -> None:
    """Inside a migration transaction: create the derived schema if selected, then use it."""
    if selection.create:
        connection.execute(_sql(f"CREATE SCHEMA {_quote(selection.schema)}"))
    connection.execute(_sql(f"SET LOCAL search_path TO {_quote(selection.schema)}"))


def select_postgres_module_schema(
    url: str, module_id: str, *, create: bool = False
) -> ModuleSchema:
    """:func:`select_module_schema` for a configured URL, on a short-lived connection.

    With ``create`` a selected derived schema is also created, in its own transaction, and a
    connection with the module URL must land in it.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    probe = create_engine(url, poolclass=NullPool)
    try:
        selection = select_module_schema(probe, module_id)
        if create:
            create_module_schema(probe, selection)
    finally:
        probe.dispose()
    if not create or selection.in_place:
        return selection
    scoped = create_engine(selection.url(url), poolclass=NullPool)
    try:
        require_connection_schema(scoped, selection.schema)
    finally:
        scoped.dispose()
    return replace(selection, create=False)


def module_storage_config(
    config: StorageEngineConfig, module_id: str
) -> StorageEngineConfig:
    """The ``record.postgres`` config a module store uses: its schema selected and created."""
    options = dict(config.record_backend_options)
    url = str(options.get("url", "") or "").strip()
    if not url:
        return config
    selection = select_postgres_module_schema(url, module_id, create=True)
    if selection.in_place:
        return config
    options["url"] = selection.url(url)
    return replace(config, record_backend_options=options)


def _sql(statement: str) -> Any:
    from sqlalchemy import text

    return text(statement)


def _current_schema(connection: Connection) -> str | None:
    value = connection.execute(_sql("SELECT current_schema()")).scalar()
    return None if value is None else str(value)


@contextmanager
def _read_only(engine: Engine) -> Iterator[Connection]:
    """One READ ONLY transaction on a new connection, always rolled back."""
    connection = engine.connect()
    try:
        transaction = connection.begin()
        try:
            connection.execute(_sql("SET TRANSACTION READ ONLY"))
            yield connection
        finally:
            transaction.rollback()
    finally:
        connection.close()


__all__ = [
    "History",
    "ModuleSchema",
    "SchemaLayout",
    "check_module_connection",
    "check_runtime_postgres_url",
    "check_runtime_schema",
    "complete_history",
    "create_module_schema",
    "decide_module_schema",
    "derived_refusal",
    "derived_schema_name",
    "enter_module_schema",
    "module_storage_config",
    "module_url",
    "read_schema",
    "require_connection_schema",
    "runtime_refusal",
    "select_module_schema",
    "select_postgres_module_schema",
    "signature",
    "supported_owners",
]
