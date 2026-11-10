#!/bin/bash
# Boot script for the N-session parallel smoke test on one spot instance.
# Hard cost cap: the instance powers off (and so terminates) after CAP_MIN.
set -u
CAP_MIN=__CAP_MIN__
shutdown -h +$CAP_MIN
exec > >(tee /var/log/openenv-smoke.log > /dev/console) 2>&1
ulimit -n 65536
export HOME=/root DEBIAN_FRONTEND=noninteractive
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH=/root/.local/bin:$PATH
git clone --depth 1 -b __BRANCH__ https://github.com/OpenAdaptAI/OpenAdapt /opt/oa
cd /opt/oa/packages/flow
uv venv -q -p 3.12 /opt/venv
VIRTUAL_ENV=/opt/venv uv pip install -q -e '.[openenv]'
/opt/venv/bin/playwright install --with-deps chromium > /dev/null
echo "OPENENV-SMOKE-READY $(date -u +%FT%TZ)"
/opt/venv/bin/python -m openadapt_flow.rl_env.parallel environments/openenv/mockmed \
  -n __N__ --episodes __EPISODES__ --out /tmp/parallel.json > /tmp/parallel.log 2>&1
echo "OPENENV-SMOKE-EXIT $?"
echo "OPENENV-SMOKE-JSON-BEGIN"
python3 -c "import json;d=json.load(open('/tmp/parallel.json'));d['errors']=d['errors'][:5];d['not_max_reward']=d['not_max_reward'][:5];print(json.dumps(d))" || tail -20 /tmp/parallel.log
echo "OPENENV-SMOKE-JSON-END"
nproc; free -g | head -2
shutdown -h now
