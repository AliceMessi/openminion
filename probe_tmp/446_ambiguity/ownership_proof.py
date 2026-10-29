"""#446 ambiguity proof: a read-only prototype of the bounded ownership preflight.

This is evidence for a proposal, not the implementation. It never writes: every read happens in one
READ ONLY transaction with explicit schema names, and the caller checks that no transaction id was
assigned.

Rule in short:
- The manifest lists, for every owner and every revision of that owner, the tables it has after that
  revision and their exact signatures: column names, types (format_type), NOT NULL, and the primary key.
  It is generated from the code by building each revision in an empty schema.
- An owner has a *complete history* in a schema when its ledger there records a revision (runtime
  storage: `migrations` rows exactly v1..vk; a module: one `alembic_version` row from its chain and
  `om_meta.module_id` naming it, or no `alembic_version` for a store bootstrap) and every table it has
  at that revision is present with exactly that revision's signature.
- A table is OWNED by an owner only when exactly one complete history includes it. Otherwise it is
  AMBIGUOUS (two or more owners), UNKNOWN (one related owner but no complete history: a fragment, a
  partial or an altered table) or FOREIGN (an OpenMinion name with unrelated columns). Order never
  matters: every owner is evaluated, and no candidate is preferred.
- An owner proceeds only where every table it creates or reads is OWNED by it. Everything else is a
  refusal that writes nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import text

INFRA = frozenset({"core_events", "core_sidecar_rows", "sidecar_ingest_log"})
ALEMBIC_LEDGER = frozenset({"alembic_version", "om_meta"})
SAFE_NAME = re.compile(r"[A-Za-z0-9_]+")
OWNED, AMBIGUOUS, UNKNOWN, FOREIGN = "OWNED", "AMBIGUOUS", "UNKNOWN", "FOREIGN"
NOTE = "Nothing was written. OpenMinion does not move, repair or restamp these objects."


def qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


@dataclass
class State:
    owner: str
    label: str  # "v3", "0004_namespace", "store"
    kind: str  # "runtime" | "alembic" | "store"
    order: int
    required: dict  # table -> signature
    optional: dict = field(default_factory=dict)  # store-only tables, accepted when present
    source: str = "dev"  # the code that produced this shape: the checkout, or a published release

    @property
    def name(self) -> str:
        return f"{self.owner}@{self.label}" + ("" if self.source == "dev" else f" ({self.source} shape)")


@dataclass
class Manifest:
    states: dict  # owner -> [State] in history order
    runtime_versions: dict  # version -> name

    def names(self, owner: str | None = None) -> set:
        owners = [owner] if owner else list(self.states)
        return {t for o in owners for st in self.states.get(o, []) for t in (*st.required, *st.optional)}


@dataclass
class Layout:
    schema: str
    exists: bool
    tables: dict  # manifest-named tables -> signature
    all_tables: list  # every table name in the schema (used only for a derived schema)
    alembic: list | None
    om_meta: dict | None
    migrations: list | None
    objects: int
    owner_role: str | None
    usage: bool
    create: bool


def signature(cols: list, pk: list) -> tuple:
    return (tuple(sorted(cols)), tuple(sorted(pk)))


def colnames(sig: tuple) -> frozenset:
    return frozenset(c[0] for c in sig[0])


def read_layout(conn, schema: str, names: set | None) -> Layout:
    row = conn.execute(text(
        "SELECT oid, pg_get_userbyid(nspowner) FROM pg_catalog.pg_namespace WHERE nspname = :s"), {"s": schema}).first()
    if row is None:
        return Layout(schema, False, {}, [], None, None, None, 0, None, False, False)
    oid, owner_role = row
    all_tables = [r[0] for r in conn.execute(text(
        "SELECT relname FROM pg_catalog.pg_class WHERE relnamespace = :o AND relkind IN ('r','p') ORDER BY 1"), {"o": oid})]
    wanted = [t for t in all_tables if names is None or t in names or t in ALEMBIC_LEDGER]
    cols: dict = {}
    for rel, att, typ, notnull in conn.execute(text(
            "SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull "
            "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid "
            "WHERE c.relnamespace = :o AND c.relkind IN ('r','p') AND a.attnum > 0 AND NOT a.attisdropped "
            "AND c.relname = ANY(:n)"), {"o": oid, "n": wanted}):
        cols.setdefault(rel, []).append((att, typ, bool(notnull)))
    pks: dict = {}
    for rel, att in conn.execute(text(
            "SELECT c.relname, a.attname FROM pg_catalog.pg_index i "
            "JOIN pg_catalog.pg_class c ON c.oid = i.indrelid "
            "JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indisprimary AND c.relnamespace = :o AND c.relname = ANY(:n)"), {"o": oid, "n": wanted}):
        pks.setdefault(rel, []).append(att)
    tables = {t: signature(cols.get(t, []), pks.get(t, [])) for t in wanted if t not in ALEMBIC_LEDGER}

    def has(table: str, *columns: str) -> bool:
        return table in cols and set(columns) <= {c[0] for c in cols[table]}

    # Ledger rows are read only from tables with the ledger's columns; any other shape matches no owner.
    alembic = om_meta = migrations = None
    if "alembic_version" in all_tables:
        alembic = (sorted(r[0] for r in conn.execute(text(f"SELECT version_num FROM {qi(schema)}.alembic_version")))
                   if has("alembic_version", "version_num") else ["<alembic_version with other columns>"])
    if "om_meta" in all_tables:
        om_meta = ({r[0]: r[1] for r in conn.execute(text(
            f"SELECT key, value FROM {qi(schema)}.om_meta WHERE key IN ('module_id', 'schema_head')"))}
            if has("om_meta", "key", "value") else {"module_id": "<om_meta with other columns>"})
    if "migrations" in tables:
        migrations = ([(int(r[0]), str(r[1])) for r in conn.execute(text(
            f"SELECT version, name FROM {qi(schema)}.migrations ORDER BY version"))]
            if has("migrations", "version", "name") else [(-1, "<migrations with other columns>")])
    objects = conn.execute(text(
        "SELECT (SELECT count(*) FROM pg_catalog.pg_class WHERE relnamespace = :o) "
        "+ (SELECT count(*) FROM pg_catalog.pg_proc WHERE pronamespace = :o) "
        "+ (SELECT count(*) FROM pg_catalog.pg_type WHERE typnamespace = :o AND typrelid = 0 AND typcategory <> 'A')"),
        {"o": oid}).scalar()
    usage = bool(conn.execute(text("SELECT has_schema_privilege(:s, 'USAGE')"), {"s": schema}).scalar())
    create = bool(conn.execute(text("SELECT has_schema_privilege(:s, 'CREATE')"), {"s": schema}).scalar())
    return Layout(schema, True, tables, all_tables, alembic, om_meta, migrations, int(objects), owner_role, usage, create)


# ---------------------------------------------------------------- classification


def ledger_matches(st: State, lay: Layout, manifest: Manifest) -> bool:
    if st.kind == "runtime":
        k = int(st.label[1:])
        return lay.migrations == [(v, manifest.runtime_versions[v]) for v in range(1, k + 1)]
    if (lay.om_meta or {}).get("module_id") != st.owner:
        return False
    if st.kind == "alembic":
        return lay.alembic == [st.label]
    return not lay.alembic


def mismatches(st: State, lay: Layout) -> list:
    out = []
    for t, sig in sorted(st.required.items()):
        obs = lay.tables.get(t)
        if obs is None:
            out.append(f"{t} missing")
        elif obs != sig:
            out.append(f"{t} {describe_diff(obs, sig)}")
    for t, sig in sorted(st.optional.items()):
        obs = lay.tables.get(t)
        if obs is not None and obs != sig:
            out.append(f"{t} {describe_diff(obs, sig)}")
    return out


def describe_diff(obs: tuple, sig: tuple) -> str:
    o, e = {c[0]: c for c in obs[0]}, {c[0]: c for c in sig[0]}
    parts = []
    if set(o) - set(e):
        parts.append(f"extra columns {sorted(set(o) - set(e))}")
    if set(e) - set(o):
        parts.append(f"missing columns {sorted(set(e) - set(o))}")
    changed = sorted(c for c in set(o) & set(e) if o[c] != e[c])
    if changed:
        parts.append(f"other type/NOT NULL on {changed}")
    if obs[1] != sig[1]:
        parts.append(f"primary key {list(obs[1])} instead of {list(sig[1])}")
    return "has " + "; ".join(parts) if parts else "differs"


def span(labels: list) -> str:
    return labels[0] if len(labels) == 1 else f"{labels[0]}..{labels[-1]}"


def related(t: str, sig: tuple, manifest: Manifest) -> dict:
    """Every owner revision whose table `t` relates to the observed one: equal signature, the same
    column names with other types/constraints, a strict column subset or a strict column superset."""
    rel: dict = {}
    names = colnames(sig)
    for owner, states in manifest.states.items():
        for st in states:
            exp, label = st.required.get(t), st.label
            if exp is None:
                exp, label = st.optional.get(t), "store bootstrap"
            if exp is None:
                continue
            en = colnames(exp)
            if exp == sig:
                kind = "equal"
            elif en == names:
                kind = "same columns, other types or keys"
            elif names < en:
                kind = "column subset"
            elif names > en:
                kind = "column superset"
            else:
                continue
            labels = rel.setdefault(owner, {}).setdefault(kind, [])
            if label not in labels:
                labels.append(label)
    return rel


def fmt_related(rel: dict) -> str:
    return "; ".join(f"{o} {k} {span(ls)}" for o in sorted(rel) for k, ls in rel[o].items())


@dataclass
class Classification:
    layout: Layout
    complete: dict  # owner -> State (several shapes of one revision may match; they share the label)
    incomplete_ledgers: dict  # owner -> (State, [mismatch])
    tables: dict  # table -> dict(cls, owner, related, detail)

    def owned_by(self, t: str, owner: str) -> bool:
        c = self.tables.get(t)
        return bool(c) and c["cls"] == OWNED and c["owner"] == owner


def classify(manifest: Manifest, lay: Layout) -> Classification:
    matched: dict = {}
    incomplete = {}
    for owner, states in manifest.states.items():
        for st in states:
            if not ledger_matches(st, lay, manifest):
                continue
            miss = mismatches(st, lay)
            if miss:
                incomplete.setdefault(owner, (st, miss))
            else:
                matched.setdefault(owner, []).append(st)
    complete = {owner: sts[0] for owner, sts in matched.items()}
    incomplete = {o: v for o, v in incomplete.items() if o not in matched}
    explained: dict = {}
    for owner, sts in matched.items():
        # Several shapes of one owner's recorded revision can match (the same revision as written by
        # different releases); they are one owner, so their tables are unioned, never compared.
        for t in sorted({x for st in sts for x in (*st.required, *(y for y in st.optional if y in lay.tables))}):
            explained.setdefault(t, []).append(" | ".join(st.name for st in sts))
    tables = {}
    for t, sig in sorted(lay.tables.items()):
        if t in INFRA or t not in manifest.names():
            continue
        rel = related(t, sig, manifest)
        claims = explained.get(t, [])
        if len(claims) == 1:
            owner = claims[0].split("@")[0]
            shared = sorted(o for o in rel if o != owner)
            detail = f"in the complete history {claims[0]}"
            if shared:
                detail += f"; the same name also relates to {fmt_related({o: rel[o] for o in shared})}, which has no complete history here"
            tables[t] = {"cls": OWNED, "owner": owner, "related": sorted(rel), "detail": detail}
        elif len(claims) > 1:
            tables[t] = {"cls": AMBIGUOUS, "owner": None, "related": sorted(rel),
                         "detail": f"in two complete histories: {', '.join(claims)}"}
        elif len(rel) > 1:
            tables[t] = {"cls": AMBIGUOUS, "owner": None, "related": sorted(rel),
                         "detail": f"no complete history includes it; relates to {fmt_related(rel)}"}
        elif len(rel) == 1:
            tables[t] = {"cls": UNKNOWN, "owner": None, "related": sorted(rel),
                         "detail": f"no complete history includes it; relates only to {fmt_related(rel)}"}
        else:
            cols = sorted(colnames(sig))
            tables[t] = {"cls": FOREIGN, "owner": None, "related": [],
                         "detail": f"OpenMinion name, unrelated columns {cols}"}
    return Classification(lay, complete, incomplete, tables)


def ledger_line(c: Classification) -> str:
    lay = c.layout
    done = ", ".join(st.name for _, st in sorted(c.complete.items())) or "none"
    bad = "; ".join(f"{st.name}: {', '.join(m)}" for _, (st, m) in sorted(c.incomplete_ledgers.items())) or "none"
    return (f"alembic_version={lay.alembic} om_meta={lay.om_meta} migrations="
            f"{None if lay.migrations is None else [v for v, _ in lay.migrations]} | complete histories: {done} | "
            f"ledger without matching tables: {bad}")


# ---------------------------------------------------------------- decisions


def _listing(c: Classification, names: list) -> str:
    return "; ".join(f"{t} {c.tables[t]['cls']}" + (f"({c.tables[t]['owner']})" if c.tables[t]["owner"] else "")
                     + f": {c.tables[t]['detail']}" for t in names)


def _incomplete_note(c: Classification, owner: str) -> str:
    if owner not in c.incomplete_ledgers:
        return ""
    st, miss = c.incomplete_ledgers[owner]
    return f" Its ledger records {st.label}, but: {', '.join(miss)}."


def decide_runtime(manifest: Manifest, c: Classification, role: str) -> tuple:
    lay = c.layout
    names = manifest.names("runtime")
    present = sorted(t for t in lay.tables if t in names)
    bad = [t for t in present if not c.owned_by(t, "runtime")]
    st = c.complete.get("runtime")
    last = max(manifest.runtime_versions)
    if not present:
        pending = list(range(1, last + 1))
    elif st is not None and not bad:
        pending = [v for v in sorted(manifest.runtime_versions) if v > int(st.label[1:])]
    else:
        return ("REFUSE", f"Runtime storage will not use schema {qi(lay.schema)}: tables it creates or reads there are "
                f"not provably its own: {_listing(c, bad)}.{_incomplete_note(c, 'runtime')} {NOTE} Back up, then "
                "relocate the other owner's objects (documented steps), or point runtime storage at another schema.")
    if pending and not lay.create:
        return ("REFUSE", f"Runtime storage needs CREATE on schema {qi(lay.schema)} for v{pending[0]}..v{pending[-1]}; "
                f"role {role} lacks it. {NOTE} A DBA can run: GRANT CREATE ON SCHEMA {qi(lay.schema)} TO {qi(role)};")
    if not pending:
        return ("PROCEED", f"runtime storage history v{st.label[1:]} is complete and exact; nothing pending")
    origin = f"from its complete history {st.label}" if st else "on a schema with no runtime storage table"
    return ("PROCEED", f"apply v{pending[0]}..v{pending[-1]} {origin}")


def module_evidence(c: Classification, m: str) -> list:
    ev = []
    if (c.layout.om_meta or {}).get("module_id") == m:
        ev.append(f"om_meta.module_id={m}")
    for t, info in c.tables.items():
        if (info["cls"] == OWNED and info["owner"] == m) or (info["cls"] != OWNED and m in info["related"]):
            ev.append(t)
    return ev


def decide_module(manifest: Manifest, m: str, cb: Classification, cd: Classification | None,
                  derived: str, db_create: bool, role: str) -> tuple:
    base = cb.layout.schema
    ev = module_evidence(cb, m)
    if cd is not None and cd.layout.exists:
        d = cd.layout
        if ev:
            return ("REFUSE", f"{m}: both {qi(base)} (holds {ev}) and {qi(derived)} exist; neither is selected. "
                    f"{NOTE} Keep one location; park the other in a schema not named <base>__<module id>.")
        if d.objects == 0:
            if d.usage and d.create:
                return ("USE_DERIVED", f"{qi(derived)} is empty (owner {d.owner_role}); role {role} has USAGE and CREATE "
                        f"there; {qi(base)} holds no {m} object")
            return ("REFUSE", f"{m}: {qi(derived)} is empty but role {role} lacks "
                    f"{'USAGE' if not d.usage else ''}{' and ' if not d.usage and not d.create else ''}"
                    f"{'CREATE' if not d.create else ''} on it (owner {d.owner_role}). {NOTE} A DBA can run: "
                    f"GRANT USAGE, CREATE ON SCHEMA {qi(derived)} TO {qi(role)}; or create it with "
                    f"CREATE SCHEMA {qi(derived)} AUTHORIZATION {qi(role)};")
        st = cd.complete.get(m)
        others = [t for t in d.all_tables if t not in INFRA and t not in ALEMBIC_LEDGER and not cd.owned_by(t, m)]
        if st is not None and not others:
            if not (d.usage and d.create):
                return ("REFUSE", f"{m}: role {role} lacks USAGE/CREATE on {qi(derived)}. {NOTE}")
            return ("USE_DERIVED", f"{qi(derived)} holds {m}'s complete history {st.label} and nothing else")
        detail = _listing(cd, [t for t in others if t in cd.tables])
        unknown = [t for t in others if t not in cd.tables]
        return ("REFUSE", f"{m}: {qi(derived)} holds objects that are not provably {m}'s: "
                f"{detail}{'; ' if detail and unknown else ''}{('not OpenMinion tables ' + str(unknown)) if unknown else ''}"
                f"{'' if st else '; no complete ' + m + ' history there'}.{_incomplete_note(cd, m)} {NOTE}")
    if not ev:
        if not SAFE_NAME.fullmatch(derived) or len(derived.encode("utf-8")) > 63:
            return ("REFUSE", f"{m}: derived schema name {derived!r} is unsafe or longer than 63 bytes. {NOTE}")
        if not db_create:
            return ("REFUSE", f"{m}: creating {qi(derived)} needs CREATE on the database; role {role} lacks it. {NOTE} "
                    f"A DBA can run: CREATE SCHEMA {qi(derived)} AUTHORIZATION {qi(role)};")
        return ("CREATE_DERIVED", f"no {m} object in {qi(base)}; create {qi(derived)}")
    st = cb.complete.get(m)
    bad = sorted(t for t in cb.tables if (t in manifest.names(m) or m in cb.tables[t]["related"])
                 and not cb.owned_by(t, m))
    if st is not None and not bad:
        later = sorted({(s.order, s.label) for s in manifest.states[m] if s.kind == st.kind and s.order > st.order})
        later = [label for _, label in later]
        if not cb.layout.usage or (later and not cb.layout.create):
            return ("REFUSE", f"{m}: role {role} lacks USAGE or CREATE on {qi(base)} for its history there. {NOTE}")
        return ("IN_PLACE", f"{m}'s complete history {st.label} in {qi(base)} stays where it is (no move, no stamp)"
                + (f"; pending {span(later)}" if later else "; nothing pending"))
    return ("REFUSE", f"{m}: {qi(base)} holds {m} objects whose ownership is not provable: "
            f"{_listing(cb, bad) if bad else 'ledger ' + str(cb.layout.om_meta)}."
            f"{'' if st else ' No complete ' + m + ' history.'}{_incomplete_note(cb, m)} {NOTE} "
            "Back up, then follow the documented relocation.")


def preflight(conn, manifest: Manifest, modules: list) -> dict:
    """Run inside a READ ONLY transaction. Returns classes, ledgers and decisions."""
    base = conn.execute(text("SELECT current_schema()")).scalar()
    role = conn.execute(text("SELECT current_user")).scalar()
    if base is None:
        return {"base": None, "decisions": {"*": ("REFUSE", f"search_path names no existing schema. {NOTE}")}}
    names = manifest.names()
    cb = classify(manifest, read_layout(conn, base, names))
    db_create = bool(conn.execute(text("SELECT has_database_privilege(current_database(), 'CREATE')")).scalar())
    out = {"base": base, "role": role, "classes": {base: cb.tables}, "ledgers": {base: ledger_line(cb)},
           "decisions": {"runtime": decide_runtime(manifest, cb, role)}}
    for m in modules:
        derived = f"{base}__{m}"
        dl = read_layout(conn, derived, names)
        cd = classify(manifest, dl) if dl.exists else None
        if cd is not None:
            out["classes"][derived] = cd.tables
            out["ledgers"][derived] = ledger_line(cd) + f" | objects={dl.objects} owner={dl.owner_role} usage={dl.usage} create={dl.create}"
        out["decisions"][m] = decide_module(manifest, m, cb, cd, derived, db_create, role)
    out["xid"] = conn.execute(text("SELECT pg_current_xact_id_if_assigned()")).scalar()
    return out
