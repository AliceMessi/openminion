#!/usr/bin/env bash
# Runs in the openminion checkout on AliceMessi/linux-runner (preset openminion,
# --postgres: PostgreSQL 16 in Docker at $OPENMINION_TEST_POSTGRES_URL).
# The checkout is dev 39da8ffb plus this probe_tmp/ folder (nothing else changed).
# Runs probe_compress_alembic_config.py with the checkout's .venv (editable install, postgres extra)
# from an empty directory. Outputs go to $RUNNER_TEMP/logs/out.
set -uo pipefail
REPO="$(pwd)"
OUT="$RUNNER_TEMP/logs/out"
mkdir -p "$OUT"
PY="$REPO/.venv/bin/python"
{ git log -1 --format='HEAD %H %s'; git log -1 --format='HEAD^ %H %s' HEAD^; git diff --stat HEAD^ HEAD; } > "$OUT/dev-commit.txt"
"$PY" -m pip show openminion > "$OUT/pip-show-dev.txt" 2>&1
"$PY" -m pip list > "$OUT/pip-list-dev.txt" 2>&1
{ uname -a; grep -E '^(PRETTY_NAME|VERSION_ID)=' /etc/os-release; "$PY" -c 'import sys,sqlite3;print("python", sys.version.split()[0], "sqlite", sqlite3.sqlite_version)'; } > "$OUT/os-dev.txt"
docker exec pg psql -U postgres -tAc 'select version()' > "$OUT/postgres-version-dev.txt" 2>&1
(cd "$REPO/probe_tmp" && sha256sum *) > "$OUT/probe-sha256-dev.txt"
EMPTY="$RUNNER_TEMP/empty-dev"
mkdir -p "$EMPTY"
cd "$EMPTY"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH
PGURL="$OPENMINION_TEST_POSTGRES_URL" "$PY" -I -X utf8 "$REPO/probe_tmp/probe_compress_alembic_config.py" "$OUT" dev > "$OUT/probe_compress_alembic_config_dev.log" 2>&1
echo "probe exit=$?" >> "$OUT/probe_compress_alembic_config_dev.log"
cat "$OUT/dev-commit.txt" "$OUT/os-dev.txt" "$OUT/postgres-version-dev.txt" "$OUT/probe_compress_alembic_config_dev.log"
