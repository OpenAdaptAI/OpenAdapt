# Reproduce the Flow 1.31.0 local evidence

This set uses the exact published `openadapt-flow` 1.31.0 wheel and the exact
release-tagged source at `2d225dea9a0ad29ca84ce1b037cc0ac671367e28`.
It uses the synthetic MockMed fixture, loopback network traffic, and a local
headless Chromium browser. It uses no paid service and no customer data.

Create a clean Python 3.12 environment. Install
`openadapt-flow[browser]==1.31.0`, `openadapt-types==0.9.0`,
`playwright==1.61.0`, and `requests`. Install Playwright Chromium revision
`1228`. Download the Flow wheel and source archive without changing them, and
check out the `v1.31.0` source tag.

Run these commands from Evals commit
`1082192e9c2ec299d31330608c1016a939d3b88d`:

```bash
python scripts/run_current_flow_local_benchmark.py \
  --flow-source /path/to/openadapt-flow-v1.31.0 \
  --flow-wheel /path/to/openadapt_flow-1.31.0-py3-none-any.whl \
  --out out/current

python scripts/run_current_flow_local_benchmark.py \
  --flow-source /path/to/openadapt-flow-v1.31.0 \
  --flow-wheel /path/to/openadapt_flow-1.31.0-py3-none-any.whl \
  --out out/current/replication

python scripts/run_flow_transaction_probe.py \
  --flow-source /path/to/openadapt-flow-v1.31.0 \
  --flow-wheel /path/to/openadapt_flow-1.31.0-py3-none-any.whl \
  --out out/current/transaction_probe

python scripts/probe_remote_lease_safety.py \
  --flow-source /path/to/openadapt-flow-v1.31.0 \
  --flow-wheel /path/to/openadapt_flow-1.31.0-py3-none-any.whl \
  --out out/current/remote_lease_safety
```

Each campaign retains its installed-package freeze, Python version,
`openadapt-types` version, browser version when applicable, and exact
Playwright Chromium revision. `EVIDENCE_MANIFEST.json` binds every verifier,
result, replication artifact, report, task/oracle contract, environment,
dependency snapshot, reliability-metric coverage, and maturity boundary.

The manifest marks every campaign as `production_acceptance: false`. These are
local synthetic or contract-fixture results. They do not establish hosted,
customer-workflow, RDP-session, or Citrix-session production acceptance.
