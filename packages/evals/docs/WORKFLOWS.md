# Workflows and repository layout

Longer runbooks that used to sit in the README, plus the package tree.

## Package tree


```
openadapt_evals/
├── flow/                 # openadapt-flow evaluation (compiler under test)
│   ├── replay_runner.py  #   demonstrate-then-replay against WAA (~0 model calls)
│   ├── hybrid_agent.py   #   compiled replay first, agent fallback on halt
│   ├── cost.py           #   model-call and dollar accounting
│   └── parallels_env.py  #   local Parallels-backed Windows env
├── harness/              # meta-benchmark harness (one driver, one metrics row)
│   ├── runner.py         #   run_meta over any Environment, verifier-scored
│   ├── protocol.py       #   Environment protocol
│   ├── adapters.py       #   BenchmarkAdapter -> Environment bridge
│   └── inspect_export.py #   export to Inspect AI format
├── agents/               # Agent implementations
│   ├── planner_grounder_agent.py  # PlannerGrounderAgent (dual-model)
│   ├── api_agent.py      #   ApiAgent (Claude, GPT) with demo persistence
│   ├── demo_guided_agent.py       # DemoGuidedAgent (demo-conditioned + self-verify)
│   ├── demo_executor.py  #   DemoExecutor (tiered deterministic replay)
│   ├── retrieval_agent.py#   RetrievalAugmentedAgent
│   ├── claude_computer_use_agent.py  # Claude computer-use agent
│   └── policy_agent.py   #   PolicyAgent (trained models)
├── adapters/             # Benchmark adapters
│   ├── base.py           #   BenchmarkAdapter ABC + data classes
│   ├── waa/              #   WAA live + mock adapters
│   ├── local/            #   LocalAdapter (native desktop, no VM)
│   ├── scrub_middleware.py#  ScrubMiddleware (PII removal, strict mode)
│   ├── rl_env.py         #   RLEnvironment (Gymnasium-style wrapper)
│   └── verl_env.py       #   verl-compatible environment wrapper
├── openenv/              # OpenEnv-compatible environment (HTTP + WebSocket)
├── training/             # RL training infrastructure
│   ├── standalone/       #   Standalone GRPO trainer (zero openadapt-ml deps)
│   ├── trl_rollout.py    #   TRL GRPOTrainer rollout_func
│   ├── areal_workflow.py #   AReaL AgentWorkflow wrapper
│   ├── trajectory_logger.py  # SFT data collection
│   └── planner_cache.py  #   pHash-based planner response cache
├── workflow/             # 4-pass workflow extraction (scrub/transcript/extract/match)
├── evaluation/           # Built-in verifiers + verifier registry
├── infrastructure/       # Azure/AWS VM and pool management
│   ├── azure_vm.py, aws_vm.py, vm_provider.py, pool.py
│   ├── ssh_tunnel.py, vm_monitor.py, resource_tracker.py
├── benchmarks/           # Evaluation runner, CLIs, viewers, trace export
├── analysis/             # Trace analysis + report generation
├── cli/                  # Unified `oa` CLI (VM lifecycle)
├── waa_deploy/           # WAA Docker image (QEMU + Windows 11 + Flask) + task setup
├── server/               # WAA server extensions (/evaluate endpoint)
├── task_config.py        # YAML/JSON custom task definitions
├── demo_library.py       # DemoLibrary (directory-based demo storage)
├── correction_*.py       # Human correction capture, store, and parsing
└── config.py             # Settings (pydantic-settings, .env)

scripts/
├── eval_flow_on_waa.py          # Evaluate openadapt-flow on WAA (dry-run default)
├── report_openadapt_performance.py   # Generate committed performance reports
├── run_current_flow_local_benchmark.py  # Local Flow benchmark
├── run_flow_transaction_probe.py     # Transaction outcome taxonomy probe
├── check_published_evidence_freshness.py  # Evidence-vs-release drift guard
├── extract_over_halt_regression.py   # Over-halt regression artifact extractor
├── run_full_eval.py             # Full evaluation runner with resume + parallel
├── collect_distillation_data.py # Teacher trajectory collection for SFT
├── finetune_distilled.py        # Student model LoRA fine-tuning
├── record_waa_demos.py          # Record demos from VNC sessions
└── train_trl_grpo.py            # TRL GRPO RL training
```

## How the WAA stack fits together

```
LOCAL MACHINE                          CLOUD VM (Azure or AWS, Ubuntu)
+---------------------+                +------------------------------+
|  oa-vm CLI          |   SSH Tunnel   |  Docker                      |
|  (pool management)  | ============>  |  +- evaluate_server (:5050)  |
|                     |  :5001 -> :5000|  |  +- /setup, /evaluate     |
|  openadapt-evals    |  :5051 -> :5050|  +- Samba share (/tmp/smb/)  |
|  (benchmark runner) |  :8006 -> :8006|  +- QEMU (Win 11)            |
|                     |                |     +- WAA Flask API (:5000) |
|                     |                |     +- Agent                 |
+---------------------+                +------------------------------+
```

Both cloud backends sit behind the same `VMProvider` protocol, so `--cloud
azure` (default) or `--cloud aws` works on any pool command. AWS supports nested
virtualization on C8i, M8i, and R8i instances; Azure uses `Standard_D8ds_v5`.
Networking and SSH tunnel details are in
[gpu_e2e_validation/architecture.md](gpu_e2e_validation/architecture.md), and
the repository's CLAUDE.md covers the Docker `--cap-add NET_ADMIN` requirement
and the port 5050 socat bridge.

## Demo-conditioned evaluation

Record demos on a remote VM over VNC, annotate them with a VLM, then evaluate:

```bash
# 1. Pre-flight: check the required apps are installed
python scripts/record_waa_demos.py record-waa \
  --tasks 04d9aeaf,0a0faba3 --server http://localhost:5001 --verify

# 2. Record interactively (act on VNC, press Enter after each step)
python scripts/record_waa_demos.py record-waa \
  --tasks 04d9aeaf,0a0faba3 --server http://localhost:5001 --output waa_recordings/

# 3. Annotate the recordings with a VLM
python scripts/record_waa_demos.py annotate \
  --recordings waa_recordings/ --output annotated_demos/ --provider openai

# 4. Run the demo-conditioned eval
python scripts/record_waa_demos.py eval \
  --demo_dir annotated_demos/ --tasks 04d9aeaf,0a0faba3
```

## Full evaluation runner

`scripts/run_full_eval.py` handles resume, per-task error isolation, health
checks with exponential backoff, and parallel pool execution:

```bash
# List tasks without executing
python scripts/run_full_eval.py --dry-run --server-url http://localhost:5001

# Single VM, all WAA tasks, API grounder
python scripts/run_full_eval.py \
    --server-url http://localhost:5001 --grounder-model gpt-4.1-mini

# Parallel across pool VMs
python scripts/run_full_eval.py --grounder-model gpt-4.1-mini --parallel 3
```

## Dedicated grounder endpoint (UI-Venus)

Serving a purpose-built GUI grounding model raises click accuracy over general
VLM grounding. Put
[UI-Venus-1.5-8B](https://huggingface.co/inclusionAI/UI-Venus-1.5-8B) on a GPU
and point `DemoExecutor` or `PlannerGrounderAgent` at it.

```bash
# On a GPU machine (A10G 24GB, RTX 4090, and similar):
bash scripts/serve_ui_venus.sh   # serves at http://0.0.0.0:8000 by default
curl http://gpu-host:8000/v1/models

# Then run the full evaluation against it
python scripts/run_full_eval.py \
    --server-url http://localhost:5001 --grounder-endpoint http://gpu-host:8000
```

The endpoint speaks the UI-Venus native bounding-box prompt format
(`[x1,y1,x2,y2]`) and works with vLLM, Ollama, or any OpenAI-compatible server.

## GRPO training with TRL

Research-stage. Trains VLM desktop agents with TRL's `GRPOTrainer` and dense
milestone rewards from WAA environments:

```bash
# Against a live WAA VM
python scripts/train_trl_grpo.py \
    --task-dir ./example_tasks --server-url http://localhost:5001 \
    --model Qwen/Qwen2.5-VL-7B-Instruct --output ./grpo_output

# Mock mode validates the whole pipeline without a VM or a GPU
python scripts/train_trl_grpo.py --task-dir ./example_tasks --mock --output ./grpo_output_mock
```

Flags worth knowing: `--constrained-decoding` (Outlines regex, which removes
unparseable output), `--vision-loss-mode` (exclude, include, or checkpoint),
`--weave-project` for Weave tracing, `--use-vllm` for faster generation, and
`--loss-type` (grpo, dapo, or dr_grpo).

## Custom agents

Implement `BenchmarkAgent` to evaluate your own agent through the same harness:

```python
from openadapt_evals import BenchmarkAgent, BenchmarkAction, BenchmarkObservation, BenchmarkTask

class MyAgent(BenchmarkAgent):
    def act(
        self,
        observation: BenchmarkObservation,
        task: BenchmarkTask,
        history: list[tuple[BenchmarkObservation, BenchmarkAction]] | None = None,
    ) -> BenchmarkAction:
        return BenchmarkAction(type="click", x=0.5, y=0.5)

    def reset(self) -> None:
        pass
```

## Related projects

| Project | Description |
|---|---|
| [OpenAdapt](https://github.com/OpenAdaptAI/openadapt) | Launcher for the governed demonstration compiler (`pip install openadapt`) |
| [openadapt-flow](https://github.com/OpenAdaptAI/openadapt-flow) | The demonstration compiler itself (record, compile, deterministic replay) |
| [openadapt-ml](https://github.com/OpenAdaptAI/openadapt-ml) | Training and policy runtime |
| [openadapt-capture](https://github.com/OpenAdaptAI/openadapt-capture) | Screen recording and demo sharing |
| [openadapt-consilium](https://github.com/OpenAdaptAI/openadapt-consilium) | Multi-model consensus library |
| [openadapt-grounding](https://github.com/OpenAdaptAI/openadapt-grounding) | UI element localization |

Lifecycle labels for every repository are in the
[repository lifecycle registry](https://github.com/OpenAdaptAI/.github/blob/main/REPOSITORY_LIFECYCLE.md).
