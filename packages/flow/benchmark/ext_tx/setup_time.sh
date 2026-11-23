#!/bin/bash
# Time from a fresh virtual environment to the first dry-run diff through
# `openadapt-flow proxy`, on the zenbook's WSL (PostgreSQL 16 local).
# The wheels are built from this worktree first (a stand-in for PyPI, not timed);
# the install is timed with no package cache.
set -euo pipefail
cd "$(dirname "$0")/../.."          # packages/flow
UV=${UV:-$HOME/.local/bin/uv}
out=$(mktemp -d)
$UV build -q --wheel --out-dir "$out" . >/dev/null
$UV build -q --wheel --out-dir "$out" ../types >/dev/null
db=oa_first_diff
dropdb --if-exists $db; createdb $db
psql -qAt -d $db -c "CREATE TABLE users (id serial PRIMARY KEY, name text, role text, active boolean DEFAULT true);
  INSERT INTO users (name, role) SELECT 'u' || g, CASE WHEN g % 150 = 0 THEN 'admin' ELSE 'member' END FROM generate_series(1, 1000) g;"
fresh=$(mktemp -d)
t0=$(date +%s.%N)
$UV venv -q --python 3.12 "$fresh/venv"
$UV pip install -q --no-cache --python "$fresh/venv/bin/python" "$out"/openadapt_types-*.whl "$out"/openadapt_flow-*.whl 'psycopg[binary]'
t1=$(date +%s.%N)
OPENADAPT_TX_STATE="$fresh/state" OPENADAPT_SEAL_HOME="$fresh/seal" \
  "$fresh/venv/bin/openadapt-flow" proxy --postgres "dbname=$db host=/var/run/postgresql" --flag users.role=admin \
  -- "$fresh/venv/bin/python" benchmark/ext_tx/first_diff_client.py
t2=$(date +%s.%N)
python3 -c "print(f'install_s={$t1-$t0:.1f} first_diff_s={$t2-$t1:.1f} total_s={$t2-$t0:.1f}')"
dropdb --if-exists $db; rm -rf "$fresh" "$out"
