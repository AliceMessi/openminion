#!/usr/bin/env bash
# #446 ambiguity proof on the GitHub Linux runner (AliceMessi/linux-runner, preset openminion, --postgres).
# The runner checks out this commit (dev 37fa75ff plus this folder), runs `make dev-install` with the
# postgres extra and starts postgres:16.
# 1. The published 0.1.7 (the last release before the session baseline gained cron_scheduler_control) is
#    installed in a separate uv venv; the probe builds its per-revision manifest and two histories with it.
# 2. The probe runs with the checkout's .venv: the checkout's manifest, the 0.1.7 shapes that differ,
#    and every case. Both run with `python -I` from an empty directory.
set -uo pipefail
OUT="$RUNNER_TEMP/logs/out"; mkdir -p "$OUT"
D=probe_tmp/446_ambiguity
P="$GITHUB_WORKSPACE/$D/probe_446_ambiguity.py"
sha256sum "$D/ownership_proof.py" "$D/probe_446_ambiguity.py" "$D/run_on_runner.sh" | tee "$OUT/probe-sha256.txt"
git log -1 --format='%H %s' | tee "$OUT/head-commit.txt"
git log -1 --format='%H %s' 37fa75ffcc6ccb02c896c89a067414c34dba55eb | tee "$OUT/dev-commit.txt"
git diff --stat 37fa75ffcc6ccb02c896c89a067414c34dba55eb HEAD | tee "$OUT/probe-commit-files.txt"
docker exec pg psql -U postgres -Atc 'select version()' | tee "$OUT/postgres-version.txt"
(. /etc/os-release && echo "$PRETTY_NAME") | tee "$OUT/os.txt"
.venv/bin/python -m pip freeze > "$OUT/pip-freeze-checkout.txt" 2>/dev/null
EMPTY="$RUNNER_TEMP/empty"; mkdir -p "$EMPTY"
python -m pip install -q uv >/dev/null 2>&1
uv venv -q --python 3.11 "$RUNNER_TEMP/v017"
uv pip install -q --python "$RUNNER_TEMP/v017/bin/python" "openminion[postgres]==0.1.7" || echo "install 0.1.7 failed"
uv pip freeze --python "$RUNNER_TEMP/v017/bin/python" > "$OUT/pip-freeze-0.1.7.txt" 2>/dev/null
(cd "$EMPTY" && "$RUNNER_TEMP/v017/bin/python" -I -X utf8 "$P" --release 0.1.7 "$OUT/release_0.1.7.json" \
    > "$OUT/release_0.1.7.log" 2>&1)
echo "release 0.1.7 exit=$?"
grep -E '^(VERSIONS|MANIFEST|STEP|WARNING|ERROR) ' "$OUT/release_0.1.7.log" | cut -c1-400
(cd "$EMPTY" && EXTRA_MANIFESTS="0.1.7=$OUT/release_0.1.7.json" "$GITHUB_WORKSPACE/.venv/bin/python" -I -X utf8 "$P" \
    "$OUT/probe_446_ambiguity.json" > "$OUT/probe_446_ambiguity.log" 2>&1)
rc=$?
echo "probe exit=$rc"
grep -E '^(VERSIONS|MANIFEST|RELEASE|IDENTICAL|IDENTICAL_WITH_RELEASES|WARNING|CASE|DECIDE|UNCHANGED|TODAY|ERROR) ' "$OUT/probe_446_ambiguity.log" | cut -c1-600
exit $rc
