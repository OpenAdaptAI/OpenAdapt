# CLI reference and configuration

Verified against openadapt-evals 0.94.1, this branch's source.

## Console entry points

The package installs nine: `openadapt-evals`, `oa`, `oa-vm`,
`openadapt-eval-flow`, `openadapt-eval`, `openadapt-train-grpo`,
`openadapt-collect`, `openadapt-analyze`, and `openadapt-gpu` for GPU instance
lifecycle.

## Benchmark CLI (`openadapt-evals`)

| Command | Description |
|---|---|
| `mock` | Run with the mock adapter, no VM required |
| `run` | Simplified live evaluation, `localhost:5001` by default |
| `live` | Run against a WAA server with full control |
| `probe` | Check whether a WAA server is reachable |
| `view` | Generate the HTML results viewer |
| `compare` | Generate a comparison viewer across several runs |
| `estimate` | Estimate Azure costs |
| `azure` | Run Azure-based parallel evaluation |
| `eval-suite` | Full-cycle evaluation: create VM, run the task-by-condition matrix, compare, clean up |
| `vm-start`, `vm-stop`, `vm-status`, `vm-debug` | Azure VM lifecycle |
| `server-start`, `up`, `vnc` | Start the WAA server, start VM plus server, open a VNC tunnel |
| `smoke-live` | End-to-end smoke test with auto-deallocate |
| `dashboard`, `azure-monitor` | VM usage dashboard, Azure ML job monitoring |
| `wandb-demo`, `wandb-report`, `wandb-log` | Weights and Biases integration |

## VM lifecycle CLI (`oa`)

`oa` nests its evaluation commands under `oa evals`:

```
oa evals {vm,run,mock,probe,view,tasks}
oa evals vm {setup,status,start,stop,deallocate,delete,probe,logs,diag,ssh,vnc,exec,monitor}
```

So single-VM setup is `oa evals vm setup`, not `oa setup`. Run `oa evals --help`
for the current list.

## Pool CLI (`oa-vm`)

The ones you need most:

| Command | Description |
|---|---|
| `pool-create` | Create N VMs with Docker and WAA |
| `pool-wait` | Wait until WAA is ready on all workers |
| `pool-run` | Distribute tasks across pool workers |
| `pool-status` | Show status of all pool VMs |
| `pool-pause` | Deallocate pool VMs and stop billing |
| `pool-resume` | Restart deallocated pool VMs |
| `pool-cleanup` | Delete all pool VMs and resources |
| `pool-exec`, `pool-logs`, `pool-vnc`, `pool-auto` | Run commands, read logs, open VNC, automate a pool run |
| `image-create` | Create a golden image from a pool VM |
| `smoke-test-aws` | Verify AWS credentials, AMI, VPC, and lifecycle |
| `gpu-setup`, `gpu-train` | Provision a GPU VM and launch verl-agent RL training |
| `azure-ml-*` | Azure ML job monitoring, logs, cost, teardown |

Most pool commands accept `--cloud azure` (the default) or `--cloud aws`;
`pool-logs`, `pool-vnc`, and `pool-exec` do not. `pool-wait`, `pool-run`, and
`pool-auto` also require `--qualification-dir`, a directory holding a fresh
`<worker>.identity.json` and `<worker>.egress.json` for each worker. Run
`oa-vm --help` for the full list.

## Flow evaluation CLI (`openadapt-eval-flow`)

Wraps `scripts/eval_flow_on_waa.py`. Dry-run unless you pass `--live`.

| Flag | Meaning |
|---|---|
| `--env {waa,parallels}` | `waa` is cloud Azure and costs money; `parallels` is a local Apple Silicon VM at $0, opt in with `OPENADAPT_PARALLELS=1` |
| `--mode {replay,hybrid}` | Compiled replay, or replay with agent fallback on halt |
| `--tasks`, `--task-ids` | Task count for the estimate, or explicit comma-separated WAA task ids |
| `--bundles` | Directory of compiled bundles, one subdirectory per task id |
| `--model` | Fallback and baseline computer-use model used for costing |
| `--fallback-rate` | Assumed fraction of tasks that halt into a paid fallback |
| `--vm-hourly` | VM dollars per hour (Azure D4_v3 = 0.19, AWS m8i.2xlarge = 0.46) |
| `--max-run-usd`, `--max-total-usd`, `--max-task-tokens`, `--billing-abort-after` | Hard guardrails enforced on any live paid run |
| `--dry-run`, `--live`, `--json`, `--run-root` | Plan only, actually run, emit JSON, choose the output root |

## Configuration

Settings load from environment variables or a `.env` file in the project root
through
[pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/).

```bash
# .env
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...

# Azure, for --cloud azure VM management
AZURE_SUBSCRIPTION_ID=...
AZURE_ML_RESOURCE_GROUP=...
AZURE_ML_WORKSPACE_NAME=...
```

### AWS authentication

AWS credentials resolve through
[boto3's default credential chain](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html).
Use SSO for interactive work:

```bash
aws configure sso   # one-time guided wizard
aws sso login       # opens a browser, caches a short-lived token
oa-vm smoke-test-aws
oa-vm pool-create --cloud aws --workers 1
```

Static keys (`AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`) also work. They
never expire, which is why they're a poor fit for interactive use. Every setting
is in [`openadapt_evals/config.py`](../openadapt_evals/config.py).

## Optional dependency groups

```bash
pip install 'openadapt-evals[training]'   # GRPO trainer + Outlines constrained decoding
pip install 'openadapt-evals[azure]'      # Azure VM management
pip install 'openadapt-evals[aws]'        # AWS EC2 management
pip install 'openadapt-evals[retrieval]'  # Demo retrieval agent
pip install 'openadapt-evals[viewer]'     # Live results viewer
pip install 'openadapt-evals[all]'        # Everything
```

The full set is `ml`, `dev`, `waa`, `flow`, `azure`, `aws`, `ocr`, `retrieval`,
`viewer`, `wandb`, `training`, `verl`, `test`, and `all`.
