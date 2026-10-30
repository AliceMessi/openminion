#!/usr/bin/env bash
# Linux runner: install openminion==0.1.16 from PyPI into a fresh venv and run
# probe_agent_run_confirmation_resume.py three times, each with a fresh work
# directory, in isolated mode from an empty directory outside any checkout.
#   bash probe_tmp/linux_setup.sh
# Outputs in $RUNNER_TEMP/logs/out: probe_runN.{json,log}, pip-freeze.txt,
# pip-install.log, probes.sha256.
set -uo pipefail
B="$(cd "$(dirname "$0")" && pwd)"
OUT="$RUNNER_TEMP/logs/out"; mkdir -p "$OUT"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH OPENMINION_STORAGE_BACKEND OPENMINION_STORAGE_POSTGRES_URL
(cd "$B" && sha256sum *.py *.sh) | tee "$OUT/probes.sha256"
python -m venv "$RUNNER_TEMP/venv"
"$RUNNER_TEMP/venv/bin/python" -m pip install -q "openminion==0.1.16" > "$OUT/pip-install.log" 2>&1
echo "pip install exit=$?"
"$RUNNER_TEMP/venv/bin/python" -m pip freeze > "$OUT/pip-freeze.txt"
EMPTY="$RUNNER_TEMP/empty"; mkdir -p "$EMPTY"
for i in 1 2 3; do
  (cd "$EMPTY" && "$RUNNER_TEMP/venv/bin/python" -B -I -X utf8 "$B/probe_agent_run_confirmation_resume.py" \
     "$RUNNER_TEMP/work-$i" "$OUT/probe_run$i.json" --expect-version 0.1.16 > "$OUT/probe_run$i.log" 2>&1)
  echo "run $i exit=$?"
  grep -E "^SUMMARY|^PASS|^FAIL|^\[a\]|^\[b\]" "$OUT/probe_run$i.log" | cut -c1-700
done
echo "empty dir after: $(ls -A "$EMPTY" | wc -l) entries"
