#!/usr/bin/env bash
# #446: installed upgrade and restore from published releases to the branch build (GitHub Linux runner,
# AliceMessi/linux-runner, preset openminion, --postgres). Usage: bash run_upgrade.sh "0.1.0 0.1.1 ..."
# The branch build is the checkout installed (not editable) into its own uv venv; each release is
# installed from PyPI into its own venv. Every probe step runs with `python -I` from an empty directory.
set -uo pipefail
RELEASES="${1:-}"
OUT="$RUNNER_TEMP/logs/out"; mkdir -p "$OUT"
D="$(cd "$(dirname "$0")" && pwd)"
P="$D/probe_446_upgrade.py"
sha256sum "$D"/probe_446_upgrade.py "$D"/run_upgrade.sh | sed "s#$D/##" | tee "$OUT/probe-sha256.txt"
git log -1 --format='%H %s' | tee "$OUT/head-commit.txt"
docker exec pg psql -U postgres -Atc 'select version()' | tee "$OUT/postgres-version.txt"
(. /etc/os-release && echo "$PRETTY_NAME") | tee "$OUT/os.txt"
python -m pip install -q uv >/dev/null 2>&1
B="$RUNNER_TEMP/venv-branch"
uv venv -q --python 3.11 "$B"
uv pip install -q --python "$B/bin/python" "$GITHUB_WORKSPACE[postgres]" > "$OUT/install-branch.log" 2>&1 || { echo "BRANCH INSTALL FAILED"; tail -20 "$OUT/install-branch.log"; exit 1; }
uv pip freeze --python "$B/bin/python" > "$OUT/pip-freeze-branch.txt"
"$B/bin/python" -I -c "import openminion, pathlib; p = pathlib.Path(openminion.__file__).parent; print(openminion.__version__, p, (p / 'modules/storage/migrations/postgres_histories.json').exists())" | tee "$OUT/branch-install.txt"
for v in $RELEASES; do
  V="$RUNNER_TEMP/venv-$v"
  uv venv -q --python 3.11 "$V"
  if ! uv pip install -q --python "$V/bin/python" "openminion[postgres]==$v" > "$OUT/install-$v.log" 2>&1; then
    echo "INSTALL FAILED $v"; continue
  fi
  uv pip freeze --python "$V/bin/python" > "$OUT/pip-freeze-$v.txt" 2>/dev/null
  (cd "$RUNNER_TEMP" && "$B/bin/python" -I -X utf8 "$P" drive "$v" "$V/bin/python" "$OUT/upgrade-$v.json" > "$OUT/upgrade-$v.log" 2>&1)
  echo "release $v exit=$? $(grep '^DONE' "$OUT/upgrade-$v.log")"
  grep '^CASE' "$OUT/upgrade-$v.log" | grep -v '| PASS |' | cut -c1-400
  rm -rf "$V"
done
