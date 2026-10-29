#!/usr/bin/env bash
# #446 PR gates on the GitHub Linux runner (AliceMessi/linux-runner, preset openminion, --postgres).
# Runs on the PR head checkout; each step's output goes to $RUNNER_TEMP/logs/out/<step>.log and
# its exit status to $RUNNER_TEMP/logs/out/status.txt. Usage: bash gates_446.sh <dev sha>
set -uo pipefail
DEV="${1:?dev sha}"
OUT="$RUNNER_TEMP/logs/out"; mkdir -p "$OUT"
PY=.venv/bin/python
PT="$PY -m pytest -p no:cacheprovider"
MARKERS="not benchmark and not e2e and not postgres and not memory_eval_benchmark and not mcp_live and not package_integration and not telegram_live and not slack_live and not slow"
NEW="tests/storage/test_postgres_schema.py tests/integration/test_postgres_module_schemas.py"
CHANGED="tests/session/test_session_store_construction.py tests/integration/test_module_baselines_postgres.py tests/integration/test_storage_migrate_report_postgres.py tests/integration/test_runtime_memory_postgres.py tests/integration/test_runtime_storage_postgres.py tests/memory/test_postgres_capture_bundle.py"
FOCUSED="$NEW $CHANGED tests/integration/test_storage_migrations_applied.py tests/storage/test_migrations_runner.py tests/storage/test_startup_validation.py tests/a2a/test_a2a_audit_postgres_conformance.py tests/a2a/test_a2a_postgres_conformance.py tests/a2a/test_audit_store_factory.py tests/memory/test_backend_factory.py tests/session/test_postgres_session_store.py tests/registry/test_registry_postgres_conformance.py tests/artifact/test_artifact_postgres_conformance.py tests/compress/test_compress_postgres_conformance.py tests/controlplane/test_controlplane_postgres_conformance.py tests/identity/test_identity_postgres_conformance.py tests/policy/test_policy_postgres_conformance.py tests/retrieve/test_retrieve_postgres_conformance.py tests/skill/test_skill_postgres_conformance.py tests/task/test_task_postgres_conformance.py tests/telemetry/test_telemetry_postgres_conformance.py"
RELATED="tests/storage tests/integration tests/memory tests/a2a tests/session tests/cli tests/registry tests/skill tests/controlplane tests/artifact tests/identity tests/policy tests/retrieve tests/task tests/telemetry tests/compress tests/secret tests/api tests/runtime tests/services"

step() {
  local name="$1"; shift
  echo "=== $name" | tee -a "$OUT/status.txt"
  ( "$@" ) > "$OUT/$name.log" 2>&1
  local rc=$?
  echo "$name exit=$rc | $(grep -E '[0-9]+ (passed|failed)|All checks passed|files? already formatted|would reformat|Error [0-9]' "$OUT/$name.log" | tail -2 | tr '\n' ' ')" | tee -a "$OUT/status.txt"
}

git log -1 --format='%H %s' | tee "$OUT/head.txt"
git log -1 --format='%H %s' "$DEV" | tee "$OUT/dev.txt"
docker exec pg psql -U postgres -Atc 'select version()' > "$OUT/postgres-version.txt"
$PY -m pip freeze > "$OUT/pip-freeze.txt" 2>/dev/null

step focused $PT -q $FOCUSED
step ci_postgres_job bash -c "$(sed -n '/Run PostgreSQL memory, skill and runtime storage conformance/,/^      - /p' .github/workflows/ci.yml | grep 'python -m pytest' | sed 's/^ *python/.venv\/bin\/python/' | sed 's/$/ \&\& echo CIJOB_STEP_OK || echo CIJOB_STEP_FAILED/')"
step postgres_all $PT -q -m postgres tests/ --ignore=tests/storage/benchmarks
step related $PT -q -m "$MARKERS" $RELATED
step ruff $PY -m ruff check .
step format make format-check
step lint make lint
git checkout -q "$DEV" -- src
git status --short src | head -5 > "$OUT/dev-src-status.txt"
step dev_src_new_and_changed $PT -q --continue-on-collection-errors $NEW $CHANGED
git checkout -q HEAD -- src
cat "$OUT/status.txt"
