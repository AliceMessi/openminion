#!/usr/bin/env bash
# Turn memory writes under `NEVER write memory records`: the probe on three installed trees,
# each installed into a fresh venv and run in isolated mode from an empty directory:
#   dev-39da8ffb     upstream dev
#   pr448-8dc45d7b   the #448 PR head (fix/448-capture-authorization, PR #471)
#   pypi-0.1.14      the published package
set -uo pipefail
B="$(cd "$(dirname "$0")" && pwd)"
OUT="$RUNNER_TEMP/logs/out"; mkdir -p "$OUT"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH OPENMINION_STORAGE_BACKEND OPENMINION_STORAGE_POSTGRES_URL
sha256sum "$B"/*.py | sed "s#$B/##" | tee "$OUT/probes.sha256"
python -m pip install -q uv >/dev/null 2>&1
TREES="dev-39da8ffb pr448-8dc45d7b pypi-0.1.14"
declare -A SPEC=(
  [dev-39da8ffb]="openminion @ git+https://github.com/openminion/openminion@39da8ffb09920792d2de269b0ad830e0623a65c9"
  [pr448-8dc45d7b]="openminion @ git+https://github.com/AliceMessi/openminion@8dc45d7b6560c18402e0d81bd0a9ac58cf2a70c8"
  [pypi-0.1.14]="openminion==0.1.14"
)
for v in $TREES; do
  echo "${SPEC[$v]}" | sed 's/.*@ //' > "$OUT/spec_$v.txt"
  uv venv -q --python 3.12 "$RUNNER_TEMP/venv-$v"
  uv pip install -q --python "$RUNNER_TEMP/venv-$v/bin/python" "${SPEC[$v]}" || echo "install $v failed"
  uv pip list --python "$RUNNER_TEMP/venv-$v/bin/python" > "$OUT/pip-list_$v.txt" 2>/dev/null
done
for v in $TREES; do
  probe=probe_turn_memory_writes
  EMPTY="$RUNNER_TEMP/empty-$v"; mkdir -p "$EMPTY"
  echo "=== $v $probe"
  (cd "$EMPTY" && "$RUNNER_TEMP/venv-$v/bin/python" -B -I -X utf8 "$B/$probe.py" "$RUNNER_TEMP/work-$v" "$OUT/${probe}_$v.json" > "$OUT/${probe}_$v.log" 2>&1)
  echo "exit=$?"
  grep -E "^SUMMARY|^PASS|^FAIL" "$OUT/${probe}_$v.log" | cut -c1-200
done
