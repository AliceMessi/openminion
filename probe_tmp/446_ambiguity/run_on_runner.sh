#!/usr/bin/env bash
# #446 ambiguity proof on the GitHub Linux runner (AliceMessi/linux-runner, preset openminion, --postgres).
# The runner checks out this commit (dev 37fa75ff plus this folder), runs `make dev-install` with the
# postgres extra and starts postgres:16. The probe runs with the checkout's .venv from an empty directory.
set -uo pipefail
OUT="$RUNNER_TEMP/logs/out"; mkdir -p "$OUT"
D=probe_tmp/446_ambiguity
sha256sum "$D/ownership_proof.py" "$D/probe_446_ambiguity.py" "$D/run_on_runner.sh" | tee "$OUT/probe-sha256.txt"
git log -1 --format='%H %s' | tee "$OUT/head-commit.txt"
git log -1 --format='%H %s' HEAD~1 | tee "$OUT/dev-commit.txt"
git diff --stat HEAD~1 HEAD | tee "$OUT/probe-commit-files.txt"
docker exec pg psql -U postgres -Atc 'select version()' | tee "$OUT/postgres-version.txt"
(. /etc/os-release && echo "$PRETTY_NAME") | tee "$OUT/os.txt"
.venv/bin/python -m pip freeze > "$OUT/pip-freeze.txt" 2>/dev/null
EMPTY="$RUNNER_TEMP/empty"; mkdir -p "$EMPTY"
(cd "$EMPTY" && "$GITHUB_WORKSPACE/.venv/bin/python" -I -X utf8 "$GITHUB_WORKSPACE/$D/probe_446_ambiguity.py" \
    "$OUT/probe_446_ambiguity.json" > "$OUT/probe_446_ambiguity.log" 2>&1)
rc=$?
echo "probe exit=$rc"
grep -E '^(VERSIONS|MANIFEST|SHARED|IDENTICAL|WARNING|CASE|DECIDE|UNCHANGED|TODAY|ERROR) ' "$OUT/probe_446_ambiguity.log" | cut -c1-600
exit $rc
