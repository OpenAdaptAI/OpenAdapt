# GPU E2E Validation Report

**Date**: 2026-03-04
**Status**: VALIDATED
**PR**: [#87](https://github.com/OpenAdaptAI/openadapt-evals/pull/87) (`feat/gpu-training-automation`)
**Author**: OpenAdapt engineering

## Summary

End-to-end validation of the verl-agent/VAGEN training pipeline on AWS
g5.xlarge (NVIDIA A10G, 24 GB VRAM). The full integration chain —
`WAADesktopEnv -> RLEnvironment -> WAALiveAdapter -> WAA Flask API` — was
confirmed working with the GPU VM connecting to an Azure WAA VM
(`waa-pool-00`) via a two-port proxy architecture. Five issues were
discovered and resolved during validation.

## Architecture

```
GPU VM (AWS g5.xlarge)                    WAA VM (Azure waa-pool-00)
+---------------------------+             +---------------------------+
|  verl-agent / VAGEN       |             |  Docker                   |
|  +- WAADesktopEnv         |   HTTP      |  +- QEMU (Windows 11)    |
|  +- RLEnvironment         | ---------> |     +- WAA Flask API      |
|  +- WAALiveAdapter        |  :5000     |     |  /screenshot        |
|                           |  :5051*    |     |  /execute_windows   |
|  PyTorch 2.8.0            |             |     +- evaluate_server   |
|  vLLM 0.11.0              |             |        /setup            |
|  Ray 2.54.0               |             |        /evaluate         |
+---------------------------+             +---------------------------+
      3.236.121.184                            172.173.66.131

* evaluate_server.py listens on port 5050 inside the Docker container.
  Docker port forwarding for 5050 is broken by QEMU NET_ADMIN, so a
  socat/nsenter UNIX socket bridge exposes it as port 5051 on the VM host.
  See architecture.md for details.
```

See [architecture.md](architecture.md) for the proxy chain deep dive.

## Environment

### GPU VM Specs

| Component       | Value                                                       |
|-----------------|-------------------------------------------------------------|
| Instance type   | g5.xlarge                                                   |
| GPU             | NVIDIA A10G Tensor Core (24 GB VRAM, Ampere, CC 8.6)       |
| vCPU            | 4 (AMD EPYC 7R13)                                          |
| Memory          | 16 GiB                                                     |
| OS              | Ubuntu 22.04 LTS                                           |
| AMI             | Deep Learning OSS Nvidia Driver AMI GPU PyTorch 2.7 (20260222) |
| Region          | us-east-1                                                   |

### Software Stack

| Package        | Version  |
|----------------|----------|
| PyTorch        | 2.8.0    |
| vLLM           | 0.11.0   |
| Ray            | 2.54.0   |
| VAGEN          | 26.2.5   |
| Transformers   | 5.2.0    |
| CUDA Toolkit   | 12.8     |
| cuDNN          | 9.10.2   |
| Python         | 3.12     |

Full version listing: [artifacts/gpu_vm_stack_versions.txt](artifacts/gpu_vm_stack_versions.txt)

## Validation Steps and Results

| # | Test                                     | Artifact Stage | Result |
|---|------------------------------------------|----------------|--------|
| 1 | GPU detected (`nvidia-smi`)              | Stage 1        | PASS   |
| 2 | Miniconda + conda env creation           | Stages 2-3b    | PASS (after TOS fix) |
| 3 | V100 -> A10G instance swap               | Stage 4        | PASS   |
| 4 | vLLM 0.11.0 install + import             | Stage 5        | PASS   |
| 5 | PyTorch 2.8.0 CUDA available             | Stage 6        | PASS   |
| 6 | VAGEN install + env registry load        | Stage 7*       | PASS   |
| 7 | Docker port 5050 socat bridge            | Stage 7        | PASS   |
| 8 | WAADesktopEnv reset + screenshot         | Stage 8        | PASS   |
| 9 | WAALiveAdapter execute action            | Stage 8        | PASS   |
| 10 | Full RLEnvironment step loop            | Stage 8        | PASS   |

\* VAGEN install output also in [artifacts/vagen_registry_output.txt](artifacts/vagen_registry_output.txt).

## Issues Discovered

| # | Issue                          | Root Cause                                          | Fix Applied                                          |
|---|-------------------------------|-----------------------------------------------------|------------------------------------------------------|
| 1 | Conda TOS error               | Miniconda 2025 requires explicit TOS acceptance     | `conda tos accept --override-channels --channel ...` |
| 2 | PyTorch version conflict       | vLLM 0.11.0 pins `torch==2.8.0`; pip pulled 2.10.0 | `pip install torch==2.8.0 --upgrade`                 |
| 3 | V100 GPU incompatible          | V100 lacks GSP (required for modern NVIDIA drivers) | Switched p3.2xlarge (V100) to g5.xlarge (A10G)      |
| 4 | Docker port 5050 broken        | QEMU `NET_ADMIN` breaks Docker bridge networking    | UNIX socket bridge via `nsenter` + `socat`           |
| 5 | AMI selection                  | Multiple DL AMI variants; wrong one wastes setup    | Standardized on OSS Nvidia Driver + PyTorch 2.7 AMI |

Details in [artifacts/e2e_test_output.txt](artifacts/e2e_test_output.txt).

## Cost

| Metric              | Value        |
|---------------------|--------------|
| Instance cost       | $1.006/hr    |
| Validation runtime  | ~30 min      |
| Estimated cost      | ~$0.50       |
| Auto-shutdown       | 30 min post-validation |

## Commits (PR #87)

```
f9e5804 feat: add GPU training automation for verl-agent E2E workflow
dda3fb2 fix: correct verl-agent Hydra config paths and document integration gap
dc4f088 fix: replace EnvironmentManagerBase with VAGEN registry-based env integration
dc1f81f fix: correct is_action_valid logic, scroll_direction, stale refs, and DRY violation
308cade fix: resolve lint errors (undefined use_fast, unused imports, f-strings)
e73df70 fix: add evaluate_url support and E2E validation test
17c919b fix: use Deep Learning AMI for GPU instances and fix setup issues
c2555ef docs: add GPU E2E validation report with artifacts
b7efb4f fix: resolve port inconsistencies and add missing context in validation docs
```

## Next Steps

1. Merge PR #87 once CI passes
2. Bump openadapt-ml PyTorch requirement to `>=2.8.0` (currently `>=2.9.1`, conflicts with vLLM)
3. Document UNIX socket bridge in deployment runbook
4. Evaluate spot instances for cost optimization during training runs
5. Run first GRPO/GiGPO training loop on validated stack
