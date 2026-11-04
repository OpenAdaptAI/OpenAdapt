#!/bin/sh
# The oracle: replay every task's reference solution against the running
# server and require the maximum reward on each.
set -eu
exec python -m openadapt_flow.rl_env.validate "$(dirname "$0")/.." \
  --base-url "${OPENENV_BASE_URL:-http://127.0.0.1:8000}" --agents scripted
