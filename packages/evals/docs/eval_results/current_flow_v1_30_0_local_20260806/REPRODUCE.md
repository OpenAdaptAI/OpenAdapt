# Reproduce the Flow 1.30.0 local evidence

This set uses the exact published `openadapt-flow` 1.30.0 wheel and the exact
release-tagged source at `0461fb86d255e681306e6403c134f08a823e70e4`.
It uses the synthetic MockMed fixture, loopback network traffic, and a local
headless Chromium browser. It uses no paid service and no customer data.

Create a clean Python 3.12 environment. Install `openadapt-flow[browser]==1.30.0`,
`openadapt-types==0.9.0`, `playwright==1.61.0`, and `requests`. Install the
Playwright Chromium binary. Download the Flow wheel without dependencies and
check out the `v1.30.0` source tag.

Run these commands from Evals commit
`cd4e8c3dfa9256dc7073c51977e1b94dee6dd00e`:

```bash
python scripts/run_current_flow_local_benchmark.py \
  --flow-source /path/to/openadapt-flow-v1.30.0 \
  --flow-wheel /path/to/openadapt_flow-1.30.0-py3-none-any.whl \
  --out out/current

python scripts/run_current_flow_local_benchmark.py \
  --flow-source /path/to/openadapt-flow-v1.30.0 \
  --flow-wheel /path/to/openadapt_flow-1.30.0-py3-none-any.whl \
  --out out/current/replication

python scripts/run_flow_transaction_probe.py \
  --flow-source /path/to/openadapt-flow-v1.30.0 \
  --flow-wheel /path/to/openadapt_flow-1.30.0-py3-none-any.whl \
  --out out/current/transaction_probe

python scripts/probe_remote_lease_safety.py \
  --flow-source /path/to/openadapt-flow-v1.30.0 \
  --flow-wheel /path/to/openadapt_flow-1.30.0-py3-none-any.whl \
  --out out/current/remote_lease_safety
```

Each campaign retains its installed-package freeze, Python version,
`openadapt-types` version, browser version when applicable, and exact Playwright
Chromium revision. `EVIDENCE_MANIFEST.json` binds every verifier, result,
replication artifact, report, task/oracle contract, environment, and dependency
snapshot.
