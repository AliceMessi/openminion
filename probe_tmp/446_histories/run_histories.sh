#!/usr/bin/env bash
# #446: measure the PostgreSQL layouts of published releases and of the checkout (GitHub Linux runner,
# AliceMessi/linux-runner, preset openminion, --postgres). Usage: bash run_histories.sh "0.1.0 0.1.1 ..."
# Each release is installed from PyPI into its own uv venv and the probe runs with `python -I` from an
# empty directory; then the checkout's .venv runs the same probe.
set -uo pipefail
RELEASES="${1:-}"
OUT="$RUNNER_TEMP/logs/out"; mkdir -p "$OUT"
D="$(cd "$(dirname "$0")" && pwd)"
P="$D/probe_446_histories.py"
sha256sum "$D"/probe_446_histories.py "$D"/run_histories.sh | sed "s#$D/##" | tee "$OUT/probe-sha256.txt"
git log -1 --format='%H %s' | tee "$OUT/head-commit.txt"
docker exec pg psql -U postgres -Atc 'select version()' | tee "$OUT/postgres-version.txt"
(. /etc/os-release && echo "$PRETTY_NAME") | tee "$OUT/os.txt"
EMPTY="$RUNNER_TEMP/empty"; mkdir -p "$EMPTY"
python -m pip install -q uv >/dev/null 2>&1
for v in $RELEASES; do
  V="$RUNNER_TEMP/venv-$v"
  uv venv -q --python 3.11 "$V"
  if ! uv pip install -q --python "$V/bin/python" "openminion[postgres]==$v" > "$OUT/install-$v.log" 2>&1; then
    echo "INSTALL FAILED $v"; tail -5 "$OUT/install-$v.log"; continue
  fi
  uv pip freeze --python "$V/bin/python" > "$OUT/pip-freeze-$v.txt" 2>/dev/null
  (cd "$EMPTY" && "$V/bin/python" -I -X utf8 "$P" "$v" "$OUT/histories-$v.json" > "$OUT/histories-$v.log" 2>&1)
  echo "release $v exit=$? $(grep -c '^STEP' "$OUT/histories-$v.log") steps, $(grep -c 'FAILED' "$OUT/histories-$v.log") failed"
  rm -rf "$V"
done
.venv/bin/python -m pip freeze > "$OUT/pip-freeze-checkout.txt" 2>/dev/null
(cd "$EMPTY" && "$GITHUB_WORKSPACE/.venv/bin/python" -I -X utf8 "$P" checkout "$OUT/histories-checkout.json" \
    > "$OUT/histories-checkout.log" 2>&1)
echo "checkout exit=$? $(grep -c '^STEP' "$OUT/histories-checkout.log") steps, $(grep -c 'FAILED' "$OUT/histories-checkout.log") failed"
grep -h 'FAILED\|^ERROR' "$OUT"/histories-*.log | cut -c1-300 | sort | uniq -c | sort -rn | head -60
