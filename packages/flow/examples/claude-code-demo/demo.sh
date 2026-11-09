#!/usr/bin/env bash
# Claude Code + OpenAdapt demo: record a form fill once, call it as an MCP tool.
#
#   ./demo.sh            install, start the expenses app, record, compile
#   ./demo.sh --by-hand  same, but you do the recording in a visible browser
#   ./demo.sh ask "Add an 18.75 expense for parking"
#                        run one Claude Code prompt against the tool
#   ./demo.sh rows       show the newest rows in the expenses table
#   ./demo.sh break      restart the app so it shows "saved" but drops writes
#   ./demo.sh fix        restart the app normally
#   ./demo.sh down       stop the app
#   ./demo.sh clean      stop the app and delete everything this script made
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PORT="${PORT:-8642}"
URL="http://127.0.0.1:${PORT}/"
VENV="$HERE/.venv"
PY="$VENV/bin/python"
WORK="$HERE/work"
DB="$WORK/expenses.db"
PIDFILE="$WORK/app.pid"
FLOW_SPEC="${FLOW_SPEC:-openadapt-flow[browser]==1.35.1}"
AGENT_SPEC="${AGENT_SPEC:-openadapt-agent==2.0.1}"

setup_venv() {
  if [ -x "$PY" ] && "$PY" -c "import openadapt_flow, openadapt_agent" 2>/dev/null; then
    return
  fi
  echo "Installing $FLOW_SPEC and $AGENT_SPEC into .venv"
  if command -v uv >/dev/null 2>&1; then
    uv venv --quiet --allow-existing --python 3.12 "$VENV"
    uv pip install --quiet --python "$PY" "$FLOW_SPEC" "$AGENT_SPEC"
  else
    local base=""
    for candidate in python3.12 python3.11 python3.10; do
      if command -v "$candidate" >/dev/null 2>&1; then base="$candidate"; break; fi
    done
    if [ -z "$base" ]; then
      echo "Need Python 3.10, 3.11, or 3.12 (or uv). Install one and rerun." >&2
      exit 1
    fi
    "$base" -m venv "$VENV"
    "$PY" -m pip install --quiet --upgrade pip
    "$PY" -m pip install --quiet "$FLOW_SPEC" "$AGENT_SPEC"
  fi
}

app_running() {
  [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null
}

stop_app() {
  if app_running; then
    kill "$(cat "$PIDFILE")"
    sleep 0.5
  fi
  rm -f "$PIDFILE"
}

start_app() {
  stop_app
  if (echo >"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
    echo "Port $PORT is already in use. Rerun with PORT=<free port> ./demo.sh" >&2
    exit 1
  fi
  mkdir -p "$WORK"
  "$PY" -u app.py --db "$DB" --port "$PORT" "$@" >"$WORK/app.log" 2>&1 &
  echo $! >"$PIDFILE"
  for _ in $(seq 1 50); do
    if (echo >"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
      head -n 1 "$WORK/app.log"
      return
    fi
    sleep 0.1
  done
  echo "The app did not start. See $WORK/app.log" >&2
  exit 1
}

show_rows() {
  "$PY" - "$DB" <<'EOF'
import sqlite3, sys
conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
rows = conn.execute(
    "SELECT id, description, amount, submitted_by, status"
    " FROM expenses ORDER BY id DESC LIMIT 5"
).fetchall()
print(f"{'id':>3}  {'description':<28} {'amount':>8}  submitted_by  status")
for r in rows:
    print(f"{r[0]:>3}  {r[1]:<28} {r[2]:>8}  {r[3]:<12}  {r[4]}")
EOF
}

case "${1:-up}" in
  up | --by-hand)
    if [ "${1:-}" = up ]; then shift; fi
    setup_venv
    start_app --reset
    "$PY" build.py --url "$URL" --db "$DB" "$@"
    ;;
  ask)
    shift
    if ! command -v claude >/dev/null 2>&1; then
      echo "Claude Code (claude) is not on PATH." >&2
      exit 1
    fi
    claude -p "$*" \
      --mcp-config "$HERE/mcp.json" --strict-mcp-config \
      --allowedTools "mcp__openadapt__*" </dev/null
    ;;
  rows) show_rows ;;
  break) start_app --drop-writes ;;
  fix) start_app ;;
  down) stop_app ;;
  clean)
    stop_app
    rm -rf "$WORK" "$HERE/mcp.json" "$VENV"
    ;;
  *)
    sed -n '2,12p' "$0"
    exit 1
    ;;
esac
