# Reproduce the Flow 1.33.0 local evidence

This set uses the exact published `openadapt-flow` 1.33.0 wheel and the exact
release-tagged source at `80dc49b7296e5e6999b04124f26a647047a14b95`.
It uses the synthetic MockMed fixture, loopback network traffic, and a local
headless Chromium browser. It uses no paid service and no customer data.

Create a clean Python 3.12 environment. Install
`openadapt-flow[console,browser]==1.33.0` (which resolved
`openadapt-types==0.10.1` and `playwright==1.62.0`) and `requests`. Install the
Playwright-managed Chromium for 1.62.0 (`playwright install chromium`,
revision `1234`). Download the Flow wheel and source archive without changing
them, verify their SHA-256 digests against PyPI, and check out the `v1.33.0`
source tag.

Run these commands from Evals commit
`aca85d431158ac517c969c5a146b38f1895c9ada`:

```bash
python scripts/run_current_flow_local_benchmark.py \
  --flow-source /path/to/openadapt-flow-v1.33.0 \
  --flow-wheel /path/to/openadapt_flow-1.33.0-py3-none-any.whl \
  --out out/current

python scripts/run_current_flow_local_benchmark.py \
  --flow-source /path/to/openadapt-flow-v1.33.0 \
  --flow-wheel /path/to/openadapt_flow-1.33.0-py3-none-any.whl \
  --out out/current/replication

python scripts/run_flow_transaction_probe.py \
  --flow-source /path/to/openadapt-flow-v1.33.0 \
  --flow-wheel /path/to/openadapt_flow-1.33.0-py3-none-any.whl \
  --out out/current/transaction_probe

python scripts/probe_remote_lease_safety.py \
  --flow-source /path/to/openadapt-flow-v1.33.0 \
  --flow-wheel /path/to/openadapt_flow-1.33.0-py3-none-any.whl \
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
