#!/usr/bin/env bash
# Two installed-package probes on three trees, each installed into a fresh venv and
# run in isolated mode from an empty directory:
#   dev-ce3b6771     upstream dev
#   pr471-6064d6d7   PR #471 head (fix/448-capture-authorization)
#   pr496-HEAD       the #496 PR head (fix/496-optional-memory-writes-policy), stacked on #471
# Probes: probe_turn_memory_writes.py (the #496 report's probe, unchanged) and
# probe_user_denied_tool_args.py (a tool call the user denies under an allowed memory policy).
set -uo pipefail
B="$(cd "$(dirname "$0")" && pwd)"
OUT="$RUNNER_TEMP/logs/out"; mkdir -p "$OUT"
PR496="${PR496:?set PR496 to the full #496 head sha}"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH OPENMINION_STORAGE_BACKEND OPENMINION_STORAGE_POSTGRES_URL
sha256sum "$B"/*.py "$B"/*.sh | sed "s#$B/##" | tee "$OUT/probes.sha256"
python -m pip install -q uv >/dev/null 2>&1
TREES="dev-ce3b6771 pr471-6064d6d7 pr496-${PR496:0:8}"
declare -A SPEC=(
  [dev-ce3b6771]="openminion @ git+https://github.com/openminion/openminion@ce3b677173e0f344d85a729113e04cf708fa6587"
  [pr471-6064d6d7]="openminion @ git+https://github.com/AliceMessi/openminion@6064d6d78dc2fee872c581805ca53dd84cca0026"
  [pr496-${PR496:0:8}]="openminion @ git+https://github.com/AliceMessi/openminion@$PR496"
)
for v in $TREES; do
  echo "${SPEC[$v]}" | sed 's/.*@ //' > "$OUT/spec_$v.txt"
  uv venv -q --python 3.12 "$RUNNER_TEMP/venv-$v"
  uv pip install -q --python "$RUNNER_TEMP/venv-$v/bin/python" "${SPEC[$v]}" || echo "install $v failed"
  uv pip list --python "$RUNNER_TEMP/venv-$v/bin/python" > "$OUT/pip-list_$v.txt" 2>/dev/null
done
for v in $TREES; do
  for probe in probe_turn_memory_writes probe_user_denied_tool_args; do
    EMPTY="$RUNNER_TEMP/empty-$v-$probe"; mkdir -p "$EMPTY"
    echo "=== $v $probe"
    (cd "$EMPTY" && "$RUNNER_TEMP/venv-$v/bin/python" -B -I -X utf8 "$B/$probe.py" "$RUNNER_TEMP/work-$v-$probe" "$OUT/${probe}_$v.json" > "$OUT/${probe}_$v.log" 2>&1)
    echo "exit=$?"
    grep -E "^SUMMARY|^PASS|^FAIL|^W \(|^P \(" "$OUT/${probe}_$v.log" | cut -c1-400
  done
done
