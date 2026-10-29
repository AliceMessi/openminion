# Runtime Database Upgrades

Status: active

The runtime database (sessions, messages, events and their derived tables) is
migrated by the runtime itself: the first process that opens it with a newer
package applies the pending runtime migrations. Each migration, including its
data backfill, runs in one transaction with its ledger entry, so a failure
leaves the database at the previous version with no partial upgrade.

Runtime migrations 13, 14 and 15 add data that every writer must maintain
from then on. A process from before such a migration does not maintain it, so
these upgrades need every writer to move together.

## Coordinated upgrade

1. Stop every process that writes the runtime database: gateways, API
   servers, workers, CLI sessions and scheduled jobs.
2. Keep a pre-upgrade backup. For a PostgreSQL runtime database use
   `pg_dump` (`openminion storage backup --backend postgres` prints the
   guidance). For SQLite, `openminion storage backup --sqlite <path>` takes a
   snapshot of one database file; back up every database of the runtime home
   as in [SQLite runtime home backup](#sqlite-runtime-home-backup).
3. Upgrade every process to the same package version, then start them. The
   first one applies the migrations.
4. Never run a process from before the upgrade against the migrated database.

Rolling back means restoring the matching pre-upgrade backup with the matching
older package. Downgrading in place is not supported: an older process ignores
the derived data and leaves it wrong.

If a migration fails, it raises, and the database stays at the previous
version. Fix the cause it reports and start again.

## PostgreSQL schemas per owner

On PostgreSQL, runtime storage and the modules used to write into the one
schema the configured URL selects. They shared `alembic_version` and
`om_meta`, and their `CREATE TABLE IF NOT EXISTS` adopted each other's
tables: `sessions`, `events`, `idempotency_keys`, `memory_records`,
`migrations`, `agents`, `session_turn_leases` and `session_retention_holds`
each have more than one owner. Since #446:

- Runtime storage keeps that schema, the *base schema*: `current_schema()`
  of the configured URL, which follows its `options`, `PGOPTIONS` and the
  role's or database's `search_path`.
- Each module uses its own schema, `<base>__<module id>`, for example
  `public__memory`. The module's connection keeps every option of the
  configured URL and appends `-c search_path="<base>__<module id>"` (a URL
  without `options` starts from `PGOPTIONS`). `openminion storage migrate
  --backend postgres`, the memory store and the A2A audit store create the
  schema inside the migration transaction, so a failed migration leaves no
  schema. The other module stores create it before their first table.
- A module whose supported history is already in the base schema keeps using
  it there. Nothing is moved or stamped. This is the case of a dedicated
  database, for example a PostgreSQL memory database or a database where
  `storage migrate --module <id>` ran alone.

This covers every PostgreSQL module writer: `openminion storage migrate`
(also with `--plan`), the startup check, the memory store, the A2A audit
and state stores, the storage module's `a2a-archive-old` command, the
session store, the controlplane store and the other module store factories.
SQLite is unchanged.

### What is checked before any write

One `READ ONLY` transaction reads only the base schema and
`<base>__<module id>`: their table names; for tables with an OpenMinion
name, the columns, types, `NOT NULL` and primary key; the ledger rows
(`migrations`, `alembic_version`, `om_meta`); the schema owner and
privileges.

An owner's history in a schema is proven only by its ledger together with
an exact match of one of its histories listed in
`src/openminion/modules/storage/migrations/postgres_histories.json`: the same
tables, each with the same columns, types, `NOT NULL` and primary key. The
ledger is the `migrations` rows v1..vk for runtime storage; for a module, it
is `om_meta.module_id` with its `alembic_version` row, or, for a module store
without Alembic, with `om_meta.schema_head`. The listed histories were
measured, not inferred. Every published package from 0.0.1 to 0.1.15 was
installed from PyPI and built each owner on PostgreSQL, and so did this
code. The file lists the histories this code builds, and the layouts that a
published package leaves (its Alembic head, a module store, or both) when an
installed upgrade from that package to this code was verified to keep their
rows and end in one of this code's histories (`upgraded_from`). Any other
layout, including the few that early 0.0.x packages left and this code
cannot upgrade, is refused as unknown. A table that only resembles a listed
one (a subset or a superset of its columns, other types) is not evidence.

- Runtime storage starts when the base schema has none of the tables it
  creates or reads, or when its `migrations` rows and every table with a
  runtime storage name match one listed history exactly, and no other owner's
  recorded history includes one of those tables.
- A module stays in the base schema when its ledger and every table with one
  of its names match one listed history exactly, and no other owner's
  recorded history includes one of those tables.
- A module uses `<base>__<module id>` when the base schema holds nothing of
  it: no `om_meta` names it, and each table with one of its names belongs to
  exactly one other owner's recorded history. An existing
  `<base>__<module id>` is used only when it is empty, or holds the module's
  proven history, or (a store that stopped before writing its ledger) holds
  only tables whose names and columns are listed for the module; the role
  needs `USAGE` and `CREATE` on it.
- Everything else is refused before any write. The message names the tables,
  the ledger recorded there and the listed histories those tables match.
  Owners that do not use the refused table names are not affected.

| Layout of the base schema | Runtime storage | Modules |
| --- | --- | --- |
| Empty, or only other applications' tables with other names | starts | create `<base>__<id>` |
| Runtime storage history only | starts | create `<base>__<id>` |
| One module's history only (dedicated database) | refused when it uses one of that module's table names | that module stays in place; the others create `<base>__<id>` |
| A module's history, then runtime storage, which committed v1..v3 and failed at v4 (memory first) | refused | memory refused (`migrations` is in both recorded histories) |
| A module's history and the empty `migrations` table of a runtime start that failed before v1 | refused | memory refused; the module stays in place |
| Runtime storage, then a module store on the same URL that adopted one of its tables (the A2A audit store and `idempotency_keys`) | starts | that module refused; a module that uses one of that module's table names (registry: `agents`) refused |
| Several modules behind one `alembic_version` (`storage migrate` for several modules on one schema) | starts unless one of its tables changed | modules whose history is not proven refused |
| A table with an OpenMinion name that matches no listed history, or that misses or adds a column | refused when it is one of its tables | modules that use the name refused |
| The module's history in the base schema, and `<base>__<id>` exists | as above | refused: two locations |
| `search_path` names no existing schema | refused | refused |
| A base schema name with other characters than letters, digits and `_`, or a `<base>__<id>` longer than 63 bytes | as above | a module that needs `<base>__<id>` refused |
| No `CREATE` on the database for the role, and no `<base>__<id>` | as above | refused; the message gives the `CREATE SCHEMA ... AUTHORIZATION` statement |

### Upgrading

1. Stop every process that writes the database, as in
   [Coordinated upgrade](#coordinated-upgrade), including module stores and
   `openminion storage migrate`.
2. Take a full `pg_dump` of the database, and check that it restores into a
   scratch database.
3. Upgrade every process to the same package. Run `openminion storage
   migrate --backend postgres` before the module stores start: a module store
   refuses a module history whose Alembic revision is behind this package's,
   because its own DDL would move the tables past the recorded revision.
   Then start the processes.
4. A refusal writes nothing. To go back, install the previous package again;
   the database is as it was.

### Relocating a refused layout

OpenMinion never moves, repairs or restamps tables itself. The steps below
are for the operator, with every OpenMinion process stopped and a verified
full `pg_dump`. Each block runs in one transaction. Indexes, constraints,
triggers and owned sequences move with their tables. The examples use the
base schema `public`.

**R1. A module's dedicated history, when runtime storage must use the base
schema.** Move the tables the refusal names as part of the module's history,
with `alembic_version` and `om_meta`, unchanged, to `<base>__<module id>`.
For a memory history at `0004_namespace` written by the memory store:

<!-- 446:R1 -->
```sql
BEGIN;
CREATE SCHEMA "public__memory";
ALTER TABLE public.memory_records SET SCHEMA "public__memory";
ALTER TABLE public.memory_candidates SET SCHEMA "public__memory";
ALTER TABLE public.memory_entities SET SCHEMA "public__memory";
ALTER TABLE public.memory_tier_transitions SET SCHEMA "public__memory";
ALTER TABLE public.memory_relations SET SCHEMA "public__memory";
ALTER TABLE public.memory_capture_bundle_receipts SET SCHEMA "public__memory";
ALTER TABLE public.migrations SET SCHEMA "public__memory";
ALTER TABLE public.alembic_version SET SCHEMA "public__memory";
ALTER TABLE public.om_meta SET SCHEMA "public__memory";
ALTER FUNCTION public.memory_records_search_trigger() SET SCHEMA "public__memory";
COMMIT;
```

The memory store then opens `public__memory` at its recorded revision, and
runtime storage starts in `public`. Before runtime storage has started in
`public`, this inverse puts the history back as it was:

<!-- 446:R1-inverse -->
```sql
BEGIN;
ALTER TABLE "public__memory".memory_records SET SCHEMA public;
ALTER TABLE "public__memory".memory_candidates SET SCHEMA public;
ALTER TABLE "public__memory".memory_entities SET SCHEMA public;
ALTER TABLE "public__memory".memory_tier_transitions SET SCHEMA public;
ALTER TABLE "public__memory".memory_relations SET SCHEMA public;
ALTER TABLE "public__memory".memory_capture_bundle_receipts SET SCHEMA public;
ALTER TABLE "public__memory".migrations SET SCHEMA public;
ALTER TABLE "public__memory".alembic_version SET SCHEMA public;
ALTER TABLE "public__memory".om_meta SET SCHEMA public;
ALTER FUNCTION "public__memory".memory_records_search_trigger() SET SCHEMA public;
DROP SCHEMA "public__memory";
COMMIT;
```

**R1b. Memory first, then runtime storage, which committed v1..v3.** The
same as R1, except that `migrations` stays in `public`: it now holds runtime
storage's rows. Runtime storage then resumes at v4.

<!-- 446:R1b -->
```sql
BEGIN;
CREATE SCHEMA "public__memory";
ALTER TABLE public.memory_records SET SCHEMA "public__memory";
ALTER TABLE public.memory_candidates SET SCHEMA "public__memory";
ALTER TABLE public.memory_entities SET SCHEMA "public__memory";
ALTER TABLE public.memory_tier_transitions SET SCHEMA "public__memory";
ALTER TABLE public.memory_relations SET SCHEMA "public__memory";
ALTER TABLE public.memory_capture_bundle_receipts SET SCHEMA "public__memory";
ALTER TABLE public.alembic_version SET SCHEMA "public__memory";
ALTER TABLE public.om_meta SET SCHEMA "public__memory";
ALTER FUNCTION public.memory_records_search_trigger() SET SCHEMA "public__memory";
CREATE TABLE "public__memory".migrations (LIKE public.migrations INCLUDING ALL);
COMMIT;
```

**R2. A module whose ledger claims another owner's table.** A ledger that
claimed another owner's table does not prove which revision the module's own
tables are at, so they are not moved into `<base>__<module id>`. Park the
module's own tables (the ones with its columns) with `alembic_version` and
`om_meta` in a holding schema that is not named `<base>__<module id>`; the
other owner's tables stay. For runtime storage, then the A2A audit store:

<!-- 446:R2-park -->
```sql
BEGIN;
CREATE SCHEMA a2a_hold;
ALTER TABLE public.audit_records SET SCHEMA a2a_hold;
ALTER TABLE public.a2a_audit_archive SET SCHEMA a2a_hold;
ALTER TABLE public.agents SET SCHEMA a2a_hold;
ALTER TABLE public.jobs SET SCHEMA a2a_hold;
ALTER TABLE public.alembic_version SET SCHEMA a2a_hold;
ALTER TABLE public.om_meta SET SCHEMA a2a_hold;
COMMIT;
```

Then run `openminion storage migrate --backend postgres --postgres-url <url>
--module a2a`, which creates `public__a2a` at its head revision, and copy the
rows with explicit column lists, setting the identity sequences past the
copied ids:

<!-- 446:R2-copy -->
```sql
BEGIN;
INSERT INTO "public__a2a".audit_records (id, record_date, ts, msg_id, trace_id, from_agent, to_agent, to_capability, type, method, status, task_id, error_code, error_message, envelope_json, data_json)
SELECT id, record_date, ts, msg_id, trace_id, from_agent, to_agent, to_capability, type, method, status, task_id, error_code, error_message, envelope_json, data_json FROM a2a_hold.audit_records;
INSERT INTO "public__a2a".a2a_audit_archive (id, record_date, source_file, ts, msg_id, trace_id, from_agent, to_agent, to_capability, type, method, status, task_id, error_code, error_message, envelope_json, data_json)
SELECT id, record_date, source_file, ts, msg_id, trace_id, from_agent, to_agent, to_capability, type, method, status, task_id, error_code, error_message, envelope_json, data_json FROM a2a_hold.a2a_audit_archive;
INSERT INTO "public__a2a".agents (agent_id, endpoint, capabilities_json, tags_json, status, updated_at)
SELECT agent_id, endpoint, capabilities_json, tags_json, status, updated_at FROM a2a_hold.agents;
INSERT INTO "public__a2a".jobs (task_id, trace_id, owner_agent_id, agent_id, method, idempotency_scope, idempotency_key, state, progress, current_step, result_inline_json, result_ref, error_json, created_at, updated_at, heartbeat_at)
SELECT task_id, trace_id, owner_agent_id, agent_id, method, idempotency_scope, idempotency_key, state, progress, current_step, result_inline_json, result_ref, error_json, created_at, updated_at, heartbeat_at FROM a2a_hold.jobs;
SELECT setval(pg_get_serial_sequence('"public__a2a".audit_records', 'id'), max(id)) FROM "public__a2a".audit_records HAVING max(id) IS NOT NULL;
SELECT setval(pg_get_serial_sequence('"public__a2a".a2a_audit_archive', 'id'), max(id)) FROM "public__a2a".a2a_audit_archive HAVING max(id) IS NOT NULL;
COMMIT;
```

Compare the row counts of each pair of tables, and keep `a2a_hold` until
the copy is verified. These statements are for the A2A tables at
`0005_job_idempotency_scope`; for an earlier revision the parked tables can
lack columns that the copy lists, and the copy stops. Then restore the dump
instead. With several modules behind one ledger, park them together and copy
each one.

**Going back after a relocation** means restoring the pre-upgrade dump with
the previous package. The only tested inverse is R1's above, and only before
runtime storage has started in the base schema. A layout where a module's
migration changed runtime storage's own tables (for example columns added to
its `memory_records` by `storage migrate --backend postgres` on the runtime
URL) has no relocation here: restore a dump from before that command.

### An empty `migrations` table

A runtime storage start that fails before v1 leaves an empty `migrations`
table; memory's baseline creates one too. An empty one proves neither, so
runtime storage and memory refuse it, and OpenMinion never adopts or drops
it. Before you remove it, find where it came from (the logs of the failed
start, or a memory history next to it) and take a full `pg_dump`.

### Permissions

Creating `<base>__<module id>` needs `CREATE` on the database. A database
administrator can instead create the empty schema for the role, which then
needs nothing more:

```sql
CREATE SCHEMA "public__memory" AUTHORIZATION openminion_app;
```

An existing schema needs `USAGE` and `CREATE` for the role:
`GRANT USAGE, CREATE ON SCHEMA "public__memory" TO openminion_app;`.

## SQLite runtime home backup

`openminion storage backup --sqlite <file> --output <backup>` snapshots one
SQLite file with SQLite's online backup API. It checks the file's
`application_id`: it accepts a file without one (`0`) or with the storage
module's (`0x4F4D0000`), and refuses any other id, which other modules set
on their own databases, with exit code 1 and `application_id mismatch for
module 'storage': expected 1330446336, found <id>`. It also keeps a copy of
the snapshot in a `<file>.bak/` directory next to the database. No single
command backs up the whole runtime home.

After its first turn, a runtime home with the default layout has these SQLite
files under its data root (by default `.openminion` under the home root).
Other configurations and features can add files.

| File | Owner (`application_id`) | `openminion storage backup` |
| --- | --- | --- |
| `state/openminion.db` (runtime database) | none | accepted |
| `state/brain/sessions.db` (brain working state) | none | accepted |
| `memory/memory.audit.db` | none | accepted |
| `ops/evidence.db`, `ops/jobs.db`, `ops/plans.db` | none | accepted |
| `task/task.db` | none | accepted |
| `artifact/index.db` | artifact | refused |
| `authored_tools/store.sqlite` | authoring | refused |
| `identity/identity.db` | identity | refused |
| `memory/memory.db`, `state/brain/memory.db` | memory | refused |
| `policy/policy.db` | policy | refused |
| `retrieve/retrieve.db` | retrieve | refused |
| `skill/skills.db` | skill | refused |
| `state/brain/brain.db` | brain | refused |
| `telemetry/telemetry.db` | telemetry | refused |

To back up the runtime home, stop every process that writes it, as in
[Coordinated upgrade](#coordinated-upgrade), then:

1. Back up each accepted file with
   `openminion storage backup --sqlite <file> --output <backup>`.
2. Back up every other SQLite file with SQLite's backup API, from the
   `sqlite3` shell:

   ```bash
   sqlite3 <file> ".backup '<backup>'"
   ```

   or from the Python environment that runs OpenMinion:

   ```bash
   python -c "import sqlite3, sys; src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2]); src.backup(dst); dst.close(); src.close()" <file> <backup>
   ```

3. Copy the other files under the data root as they are, except the
   `<file>-journal`, `<file>-wal` and `<file>-shm` companions of the SQLite
   files. Steps 1 and 2 open each database through SQLite, which rolls back a
   hot `-journal` and reads a `-wal`, so the backups already hold the
   committed content. A companion copied now and put back next to a restored
   file would be applied to it.
4. Record a checksum of each backup, for example with `sha256sum`.

To roll back:

1. Stop every process that uses the runtime home, readers included.
2. Check every backup before you replace anything: it matches the checksum
   you recorded, and `PRAGMA integrity_check` on it, for example
   `sqlite3 <backup> "PRAGMA integrity_check"`, prints `ok`.
3. Move each SQLite file of the runtime home, together with its
   `<file>-journal`, `<file>-wal` and `<file>-shm` if they exist, to a
   directory outside the data root, keeping their paths relative to the data
   root. Keep each set together and do not delete any of it: a writer that
   did not finish can leave a `-journal` or a `-wal` that is the only copy of
   that database's last committed content, and SQLite applies it to whatever
   file has the database's name. Left next to a restored file, a hot
   `-journal` would be rolled back into the backup and a `-wal` would add
   newer commits to it.
4. Put each backup in place of its file: restore an accepted file with
   `openminion storage restore --sqlite <file> --snapshot <backup> --yes`,
   and copy the backup to the file's path for the others. Then copy the other
   files back, without any `-journal`, `-wal` or `-shm`.
5. Check that no `<file>-journal`, `<file>-wal` or `<file>-shm` is next to a
   restored file before you start any process.

## Brain working state fields

The brain saves each session's working state as JSON: in the `working_state`
table of the brain's session store (`brain/sessions.db` next to the SQLite
runtime database, `state/brain/sessions.db` in the default layout), or in
`working_state.json` with the local session store. The working state rejects
fields it does not define.

Packages with `turn_evidence` and `turn_evidence_trace_id` add both fields,
as `null` when no same-turn continuation ran, when the brain saves its typed
working state (`save_state`). A writer that updates the stored JSON in place
keeps its keys. Before each runtime turn reaches the brain loop, the
turn-start reset rewrites the stored state that way, and so does the
injection of a gateway system context or of task, cron, project, watch,
consolidation or delegation hints when the turn carries them. So on a session
last saved before the fields, the first runtime turn after the upgrade can
write the reset's row without them, and the brain's next typed save in that
turn adds them. Once a typed save has added them, the rewrites keep them. A
row without the fields therefore does not mean that an older process wrote
it.

Packages with the fields read a working state without them, including those
rows, with both unset. A package from before the fields rejects any working
state saved with them: validation fails with `extra_forbidden` for both.

Upgrade the processes that share the runtime state as in
[Coordinated upgrade](#coordinated-upgrade): stop them, keep a pre-upgrade
backup (the databases of the runtime home as in
[SQLite runtime home backup](#sqlite-runtime-home-backup), and with the local
session store a copy of its `sessions` directory), upgrade them together, and
never run an older process against working state saved by the newer package.
Rolling back means restoring the matching pre-upgrade backup with the
matching older package.

## Runtime migration 15: session metadata string rows

Migration 15 adds `session_metadata_strings`, one row for each top-level member
of a session's stored metadata whose value is a string. The session store
writes these rows in the same transaction as every session insert, metadata
update and delete. The migration backfills existing sessions in batches of
1,000 inside its transaction. It reads each stored `metadata_json` with
`json.loads`, so documents PostgreSQL cannot parse (NaN, Infinity, an escaped
NUL or a lone surrogate) get their rows too, and it does not change any stored
metadata. A session whose `metadata_json` is not JSON stops the migration,
which rolls back and names that session.

As measured on 50,000 sessions with PostgreSQL 16, the backfill wrote 160,052
rows in 4.8 s, the table and its indexes took 33 MB next to 21 MB for
`sessions`, and a metadata write took 1.27 ms instead of 0.64 ms. The cost
grows with the number of string members per session.

A process from before migration 15 does not maintain the rows, which affects
PostgreSQL metadata filters (`list_sessions(metadata_filter=...)`, used by ACP
`session/list` and Focus directory sessions):

- **Missing match.** A session it creates has no rows, so no filter returns
  it.
- **Stale match.** A session whose metadata it changes keeps the rows of its
  earlier metadata, so a filter can return it for a value it no longer has,
  and miss it for its new value.

SQLite metadata filters do not read these rows.

## Metadata filter behavior by backend

PostgreSQL selects a session whose stored metadata, as
`SessionRecord.metadata` reads it after the JSON round trip, has a top-level
member named exactly as the filter key whose value is a string equal to the
filter value. Keys keep the existing `strip()`, and empty keys are skipped.
Every other character of a key or value is literal, including `.`, `[`, `"`,
non-ASCII text, NUL and lone surrogates. Numbers, booleans, null, objects,
arrays and missing members never match. The query returns at most `limit`
sessions in the usual `updated_at, id` order.

Before either backend reads a filter value, `list_sessions` keeps its existing
normalization: a false value (`None`, `0`, `False`, an empty container) is
sent as `""`, so `{"surface": None}` matches a session whose `surface` is the
empty string, on both backends. PostgreSQL then raises `TypeError` for any
other filter value that is not a string.

The rows store each name and value as its JSON string text (`json.dumps`,
ASCII without a NUL byte). PostgreSQL compares that text with `COLLATE "C"`,
which is a byte comparison, so the database's default collation does not
affect which sessions match. The indexed `entry_digest` only finds the
candidate rows; two different entries with one digest still compare their
full name and value.

SQLite keeps its existing `json_extract(metadata_json, '$.' || key) = value`
reader. It differs from the PostgreSQL behavior where it reads more than a
literal top-level string:

- it reads a key as a JSON path (`a.b`, `a[0]`, a leading `"`);
- it matches an object or array by its minified JSON text;
- it matches keys that `json.dumps` escapes (a quote, a backslash, control or
  non-ASCII characters), and values with a NUL, on SQLite 3.45.1, 3.46.1 and
  3.47.1 but not on 3.40.1 or 3.34.1;
- it raises `malformed JSON` on NaN and Infinity documents on SQLite 3.40.1
  and 3.34.1;
- for a member name stored twice (which the writer produces only for two
  distinct float NaN keys), it reads the first value, where PostgreSQL and
  `json.loads` read the last;
- it compares a filter value that is not a string (other than a false value,
  which becomes `""`) as an SQL value, so `1` matches a stored `1` or `true`,
  where PostgreSQL raises `TypeError`.

## PostgreSQL runtime storage

With `storage.backend: postgres` and `storage.postgres_url` (or
`OPENMINION_STORAGE_BACKEND=postgres` and `OPENMINION_STORAGE_POSTGRES_URL`),
`APIRuntime` keeps its runtime record store (sessions, messages, events and
room participants) in PostgreSQL. This section lists only the paths that
`tests/integration/test_runtime_storage_postgres.py` checks against a
PostgreSQL server; it is not a backend support matrix.

Checked on PostgreSQL:

- `APIRuntime.from_config_path` start and `close()`: no server connection is
  left after `close()`, and no runtime SQLite database is created at
  `storage.path`;
- a session and a message written through `runtime.sessions` are in
  PostgreSQL, and one echo-agent turn through `Agent(runtime=...)` completes
  and stores its messages there;
- `openminion room create`: the room and its owner participant row are in
  PostgreSQL;
- `OpenMinionRuntime.from_config_path` start and `close()`.

SQLite only:

- `APIRuntime.storage_connection` is the runtime record store's
  `sqlite3.Connection` on SQLite, and `None` for any other backend;
- the compatibility wrapper's `OpenMinionRuntime.storage` is the same value.

The paths above reach the runtime database through `runtime_storage` and
`sessions`, not through `storage_connection`.

Not covered:

- other runtime surfaces on PostgreSQL runtime storage;
- `openminion storage migrate --backend postgres` and module PostgreSQL
  migrations, except where each owner's tables go: see
  [PostgreSQL schemas per owner](#postgresql-schemas-per-owner);
- the module stores under the data root (for example `brain/sessions.db` and
  `memory/memory.db`): they are SQLite files on both backends, because
  `storage.backend` does not select them.
