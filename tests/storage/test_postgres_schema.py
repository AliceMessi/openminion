"""PostgreSQL schema per owner (#446): the decisions on catalog layouts, without a server.

The layouts are built from the listed histories in ``postgres_histories.json``; the same cases run
against a real PostgreSQL server in ``tests/integration/test_postgres_module_schemas.py``.
"""

from __future__ import annotations

import pytest

from openminion.modules.storage.migrations import postgres_schema as ps
from openminion.modules.storage.migrations.errors import SchemaOwnershipError

MODULES = ("memory", "a2a", "session", "registry", "artifact", "secret")


def _history(owner: str, ledger: str, *, store: bool = True) -> ps.History:
    items = ps._catalog().histories[owner][ledger]
    current = [h for h in items if "current" in h.measured]
    return sorted(current, key=lambda h: len(h.tables), reverse=store)[0]


def _runtime_rows(k: int) -> tuple[tuple[int, str], ...]:
    names = ps._catalog().runtime_versions
    return tuple((v, names[v]) for v in range(1, k + 1))


def _layout(name: str = "public", **kwargs) -> ps.SchemaLayout:
    tables = dict(kwargs.pop("tables", {}))
    kwargs.setdefault("objects", len(tables))
    return ps.SchemaLayout(
        name=name,
        exists=True,
        tables=tables,
        owner="app",
        usage=True,
        create=True,
        **kwargs,
    )


def _runtime(k: int = 15, **extra: str) -> dict[str, str]:
    return {**_history("runtime", f"v{k}").tables, **extra}


def _memory_meta() -> dict[str, str]:
    return {"module_id": "memory", "schema_head": "0004_namespace"}


def _decide(
    module_id: str, base: ps.SchemaLayout, derived: ps.SchemaLayout | None = None
):
    derived = derived or ps.SchemaLayout(name=f"{base.name}__{module_id}", exists=False)
    return ps.decide_module_schema(
        module_id, base, derived, role="app", database_create=True
    )


def _outcome(module_id: str, base: ps.SchemaLayout) -> str:
    try:
        selection = _decide(module_id, base)
    except SchemaOwnershipError:
        return "refused"
    return "in place" if selection.in_place else selection.schema


def test_listed_histories_name_their_tables_and_sources() -> None:
    catalog = ps._catalog()
    assert sorted(catalog.runtime_versions) == list(
        range(1, len(catalog.runtime_versions) + 1)
    )
    for owner, ledgers in catalog.histories.items():
        assert owner == ps.RUNTIME or owner in catalog.names
        for ledger, items in ledgers.items():
            for history in items:
                assert history.measured, (owner, ledger)
                assert all(";" in sig for sig in history.tables.values())
    assert "core_events" in catalog.engine_tables


def test_fresh_base_schema_runtime_proceeds_and_modules_create_their_own() -> None:
    base = _layout()

    assert ps.runtime_refusal(base) is None
    for module_id in MODULES:
        selection = _decide(module_id, base)
        assert (selection.schema, selection.create) == (f"public__{module_id}", True)


def test_runtime_history_keeps_its_tables_and_modules_go_to_their_schema() -> None:
    base = _layout(tables=_runtime(15), migrations=_runtime_rows(15))

    assert ps.runtime_refusal(base) is None
    # sessions, events, idempotency_keys, memory_records and migrations are runtime
    # storage's here: its ledger and its exact v15 history prove it.
    assert {m: _outcome(m, base) for m in MODULES} == {
        m: f"public__{m}" for m in MODULES
    }


def test_runtime_history_from_an_earlier_version_proceeds() -> None:
    base = _layout(tables=_runtime(11), migrations=_runtime_rows(11))

    assert ps.runtime_refusal(base) is None


def test_dedicated_module_history_stays_in_place_and_runtime_is_refused() -> None:
    memory = _history("memory", "alembic 0004_namespace")
    base = _layout(
        tables=memory.tables,
        alembic=("0004_namespace",),
        om_meta=_memory_meta(),
        migrations=(),
    )

    assert _decide("memory", base) == ps.ModuleSchema("memory", "public", "public")
    message = ps.runtime_refusal(base)
    assert message is not None
    assert "memory_records (part of memory alembic 0004_namespace)" in message
    assert "Nothing was written" in message
    assert _outcome("session", base) == "public__session"


def test_a_table_two_recorded_histories_include_refuses_both_owners() -> None:
    # Memory first, then runtime storage, which committed v1..v3 and failed at v4:
    # memory's empty migrations table now holds runtime storage's ledger rows.
    memory = _history("memory", "alembic 0004_namespace")
    base = _layout(
        tables={**memory.tables, **_runtime(3)},
        alembic=("0004_namespace",),
        om_meta=_memory_meta(),
        migrations=_runtime_rows(3),
    )

    runtime = ps.runtime_refusal(base)
    assert runtime is not None
    assert (
        "migrations (part of runtime storage v3 and memory alembic 0004_namespace)"
        in runtime
    )
    with pytest.raises(SchemaOwnershipError, match="part of runtime storage v3 and"):
        _decide("memory", base)
    # Owners that do not touch those names are not refused.
    assert _outcome("registry", base) == "public__registry"
    assert _outcome("session", base) == "public__session"


def test_an_empty_migrations_table_stays_refused() -> None:
    # A2A first, then a runtime storage start that failed before v1.
    a2a = _history("a2a", "alembic 0005_job_idempotency_scope")
    base = _layout(
        tables={**a2a.tables, "migrations": _runtime(1)["migrations"]},
        alembic=("0005_job_idempotency_scope",),
        om_meta={"module_id": "a2a", "schema_head": "0005_job_idempotency_scope"},
        migrations=(),
    )

    assert ps.runtime_refusal(base) is not None
    with pytest.raises(SchemaOwnershipError, match="no memory ledger"):
        _decide("memory", base)
    assert _outcome("a2a", base) == "in place"
    assert _outcome("artifact", base) == "public__artifact"


def test_a_ledger_that_claims_another_owners_table_is_refused() -> None:
    # Runtime storage, then the A2A audit store on the same URL: A2A's ledger records
    # 0005, but idempotency_keys is runtime storage's.
    a2a = _history("a2a", "alembic 0005_job_idempotency_scope")
    tables = {t: s for t, s in a2a.tables.items() if t != "idempotency_keys"}
    base = _layout(
        tables={**_runtime(15), **tables},
        alembic=("0005_job_idempotency_scope",),
        om_meta={"module_id": "a2a", "schema_head": "0005_job_idempotency_scope"},
        migrations=_runtime_rows(15),
    )

    assert ps.runtime_refusal(base) is None
    with pytest.raises(
        SchemaOwnershipError, match="idempotency_keys has other columns"
    ):
        _decide("a2a", base)
    assert _outcome("memory", base) == "public__memory"


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("extra column", "sessions has other columns"),
        ("missing column", "sessions has other columns"),
        ("missing table", "room_participants is missing"),
    ],
)
def test_partial_subset_or_superset_tables_are_not_evidence(
    change: str, expected: str
) -> None:
    tables = _runtime(15)
    if change == "extra column":
        tables["sessions"] = tables["sessions"].replace(
            "; pk(", ", added_by_hand text; pk("
        )
    elif change == "missing column":
        tables["sessions"] = tables["sessions"].replace("channel text NOT NULL, ", "")
    else:
        del tables["room_participants"]
    base = _layout(tables=tables, migrations=_runtime_rows(15))

    message = ps.runtime_refusal(base)
    assert message is not None and expected in message
    # Owners that touch those names are refused too; the others are not.
    assert _outcome("session", base) == "refused"
    assert _outcome("registry", base) == "public__registry"


def test_tables_without_a_ledger_are_not_evidence() -> None:
    # Two of runtime storage's tables, exact, with no migrations rows: their columns
    # match runtime storage v10..v15 and the session module, which proves neither.
    runtime = _runtime(15)
    base = _layout(
        tables={
            t: runtime[t] for t in ("session_turn_leases", "session_retention_holds")
        }
    )

    message = ps.runtime_refusal(base)
    assert message is not None and "no runtime storage ledger" in message
    with pytest.raises(SchemaOwnershipError, match="no session ledger"):
        _decide("session", base)


def test_unknown_tables_with_owner_names_are_refused() -> None:
    base = _layout(tables={"sessions": "id integer NOT NULL; pk(id)"})

    message = ps.runtime_refusal(base)
    assert message is not None and "sessions (matches no listed history)" in message
    assert _outcome("session", base) == "refused"
    assert _outcome("memory", base) == "public__memory"


def test_a_shared_ledger_refuses_the_owners_it_does_not_prove() -> None:
    # `storage migrate --module artifact`, then `--module registry`: the shared
    # alembic_version already said 0001_baseline, so registry's tables were never
    # created and om_meta names registry.
    artifact = _history("artifact", "alembic 0001_baseline")
    base = _layout(
        tables=artifact.tables,
        alembic=("0001_baseline",),
        om_meta={"module_id": "registry", "schema_head": "0001_baseline"},
    )

    with pytest.raises(SchemaOwnershipError, match="agents is missing"):
        _decide("registry", base)
    with pytest.raises(SchemaOwnershipError, match="no artifact ledger"):
        _decide("artifact", base)
    assert ps.runtime_refusal(base) is None
    assert _outcome("secret", base) == "public__secret"


def test_both_locations_are_refused() -> None:
    memory = _history("memory", "alembic 0004_namespace")
    base = _layout(
        tables=memory.tables,
        alembic=("0004_namespace",),
        om_meta=_memory_meta(),
        migrations=(),
    )
    derived = _layout("public__memory", objects=0)

    with pytest.raises(SchemaOwnershipError, match="neither is selected"):
        _decide("memory", base, derived)


def test_existing_derived_schema_rules() -> None:
    base = _layout()
    memory = _history("memory", "alembic 0004_namespace")

    empty = _layout("public__memory", objects=0)
    assert _decide("memory", base, empty) == ps.ModuleSchema(
        "memory", "public", "public__memory"
    )
    own = _layout(
        "public__memory",
        tables={**memory.tables, "core_events": None},
        alembic=("0004_namespace",),
        om_meta=_memory_meta(),
        migrations=(),
    )
    assert _decide("memory", base, own).schema == "public__memory"
    # A store bootstrap that stopped before writing om_meta: every table is the
    # module's, by name and exact columns.
    partial = _layout(
        "public__registry",
        tables={
            "agents": _history("registry", "alembic 0001_baseline").tables["agents"],
            "core_events": None,
        },
    )
    assert _decide("registry", base, partial).schema == "public__registry"

    refusals = {
        "foreign table": _layout("public__memory", tables={"notes": None}),
        "objects but no table": _layout("public__memory", objects=2),
        "other ledger": _layout(
            "public__memory",
            tables=memory.tables,
            alembic=("0004_namespace",),
            om_meta={"module_id": "a2a"},
        ),
        "other columns": _layout(
            "public__memory",
            tables={**memory.tables, "memory_records": "id text NOT NULL; pk(id)"},
            alembic=("0004_namespace",),
            om_meta=_memory_meta(),
        ),
        "no privileges": ps.SchemaLayout(
            name="public__memory", exists=True, owner="postgres", usage=True
        ),
    }
    for case, derived in refusals.items():
        with pytest.raises(SchemaOwnershipError):
            _decide("memory", base, derived)
        assert case


def test_privilege_and_name_refusals_name_the_fix() -> None:
    base = _layout()
    with pytest.raises(SchemaOwnershipError) as no_create:
        ps.decide_module_schema(
            "memory",
            base,
            ps.SchemaLayout(name="public__memory", exists=False),
            role="app",
            database_create=False,
        )
    assert 'CREATE SCHEMA "public__memory" AUTHORIZATION "app"' in str(no_create.value)

    with pytest.raises(SchemaOwnershipError) as grant:
        _decide("memory", base, ps.SchemaLayout(name="public__memory", exists=True))
    assert 'GRANT USAGE, CREATE ON SCHEMA "public__memory" TO "app"' in str(grant.value)

    for name in ("App-Base", "b" * 60):
        with pytest.raises(
            SchemaOwnershipError, match="needs its own PostgreSQL schema"
        ):
            _decide("memory", _layout(name))


def test_module_url_appends_the_schema_and_keeps_every_other_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.engine import make_url

    monkeypatch.setenv("PGOPTIONS", "-c statement_timeout=5000")
    url = (
        "postgresql+psycopg://app:secret@db:5432/om?application_name=om"
        "&options=-c%20work_mem%3D4MB&sslmode=require"
    )

    query = make_url(ps.module_url(url, "AppBase__memory")).query
    assert query["application_name"] == "om"
    assert query["sslmode"] == "require"
    assert query["options"] == '-c work_mem=4MB -c search_path="AppBase__memory"'

    plain = make_url(ps.module_url("postgresql+psycopg://app@db/om", "public__a2a"))
    assert plain.query["options"] == (
        '-c statement_timeout=5000 -c search_path="public__a2a"'
    )
    assert ps.ModuleSchema("a2a", "public", "public").url(url) == url

    with pytest.raises(SchemaOwnershipError, match="repeats the options"):
        ps.module_url(url + "&options=-c%20x%3D1", "public__a2a")
