#!/bin/bash
# Boot script for the N-session parallel smoke test on one spot instance.
# Hard cost cap: the instance powers off (and so terminates) after CAP_MIN.
# Output goes to a log file; markers and the result go to the kernel log, which
# reaches the serial console that get-console-output reads (a getty on ttyS0
# breaks direct writes to it).
set -eu
CAP_MIN=__CAP_MIN__
shutdown -h +$CAP_MIN
LOG=/var/log/openenv-smoke.log
exec >> $LOG 2>&1
say() { echo "$*"; echo "<2>OPENENV $*" > /dev/kmsg 2>/dev/null || true; }
finish() {
  code=$?
  if [ $code -ne 0 ]; then
    say "SMOKE-FAILED $code"
    tail -5 $LOG | cut -c1-200 | while read -r line; do say "LOG $line"; done
    say "SMOKE-JSON-END"
    sleep 600  # keep the console readable before shutdown
  fi
  sleep 60
  shutdown -h now
}
trap finish EXIT
say "SMOKE-BOOT $(date -u +%FT%TZ)"
ulimit -n 65536
export HOME=/root DEBIAN_FRONTEND=noninteractive
say "SMOKE-STAGE uv"
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH=/root/.local/bin:$PATH
say "SMOKE-STAGE clone"
git clone --depth 1 -b __BRANCH__ https://github.com/OpenAdaptAI/OpenAdapt /opt/oa
cd /opt/oa/packages/flow
say "SMOKE-STAGE venv"
uv venv -q -p 3.12 /opt/venv
VIRTUAL_ENV=/opt/venv uv pip install -q -e '.[openenv]'
say "SMOKE-STAGE browsers"
/opt/venv/bin/playwright install --with-deps chromium > /dev/null
say "SMOKE-READY $(nproc) cpu $(free -g | awk '/Mem:/{print $2}') GiB"
rc=0
/opt/venv/bin/python -m openadapt_flow.rl_env.parallel environments/openenv/mockmed \
  -n __N__ --episodes __EPISODES__ --out /tmp/parallel.json > /tmp/parallel.log 2>&1 || rc=$?
say "SMOKE-EXIT $rc"
say "SMOKE-JSON-BEGIN"
say "$(python3 -c "
import json; d=json.load(open('/tmp/parallel.json'))
print(json.dumps({k: d[k] for k in ('sessions','episodes','passed','reset_s','episode_s','wall_s','episodes_per_minute')} | {'errors': len(d['errors']), 'not_max_reward': len(d['not_max_reward'])}))" 2>/dev/null || echo '{"result": "missing"}')"
say "SMOKE-JSON-END"
