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
  migrations;
- the module stores under the data root (for example `brain/sessions.db` and
  `memory/memory.db`): they are SQLite files on both backends, because
  `storage.backend` does not select them.
