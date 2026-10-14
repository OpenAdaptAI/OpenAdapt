# Windows Agent Arena Integration Guide

## Overview

OpenAdapt uses Windows Agent Arena (WAA) for benchmarking GUI automation agents.
Our integration philosophy: **use vanilla WAA with minimal patches**.

## Architecture

```
+------------------+     +------------------+     +------------------+
|  openadapt-ml    |     |  Azure VM        |     |  WAA Container   |
|  CLI commands    | --> |  Docker host     | --> |  Windows VM      |
|  vm waa-native   |     |  KVM enabled     |     |  Flask server    |
+------------------+     +------------------+     +------------------+
         |                                                  |
         |                                                  v
+------------------+                              +------------------+
|  openadapt-evals |                              |  WAA Evaluators  |
|  WAALiveAdapter  | <--------------------------- |  (unmodified)    |
+------------------+    HTTP: /screenshot,        +------------------+
                        /accessibility,
                        /execute_windows,
                        /evaluate
```

## What We Patch (and Why)

We make exactly 5 lines of patches to WAA:

### 1. Modern dockurr/windows base (1 line)

**File**: `vendor/WindowsAgentArena/docker/windows-local/Dockerfile`
```dockerfile
FROM dockurr/windows:latest
```

**Why**: Microsoft's `windowsarena/windows-local:latest` is frozen from an old
dockurr/windows version that doesn't support auto-ISO download.

### 2. Auto-ISO download (1 line)

**File**: `vendor/WindowsAgentArena/scripts/run.sh`
```bash
docker_command+=" -e VERSION=11e"
```

**Why**: Enables automatic download of Windows 11 Enterprise Evaluation.
No manual ISO download required. Enterprise Eval has built-in GVLK key,
so no product key prompts.

### 3. IP address fix (3 lines)

**File**: `vendor/WindowsAgentArena/src/win-arena-container/Dockerfile-WinArena`
```dockerfile
RUN sed -i 's|20\.20\.20\.21|172.30.0.2|g' /entry_setup.sh /entry.sh /start_client.sh && \
    find /client -name "*.py" -exec sed -i 's|20\.20\.20\.21|172.30.0.2|g' {} \;
```

**Why**: Modern dockurr/windows (v5.07+) changed the Windows VM IP from
`20.20.20.21` to `172.30.0.2`. Microsoft's scripts hardcode the old IP.

## Quick Start

### Local Development (macOS/Linux with Docker + KVM)

```bash
# 1. Ensure WAA submodule is initialized
git submodule update --init --recursive

# 2. Build windows-local base (one time)
cd vendor/WindowsAgentArena
docker build -t windowsarena/windows-local:latest docker/windows-local/

# 3. Build WAA image
cd scripts
./build-container-image.sh --build-base-image true

# 4. Create golden image (first run, ~20 min)
./run-local.sh --prepare-image true

# 5. Run benchmark
./run-local.sh --model gpt-4o

# VNC available at http://localhost:8006
```

### Azure Deployment

```bash
# 1. Create VM with nested virtualization
uv run python -m openadapt_ml.benchmarks.cli vm create

# 2. Setup Docker
uv run python -m openadapt_ml.benchmarks.cli vm setup
uv run python -m openadapt_ml.benchmarks.cli vm docker-move

# 3. Run using Microsoft's scripts
uv run python -m openadapt_ml.benchmarks.cli vm waa-native --api-key $OPENAI_API_KEY

# 4. Monitor (VNC via SSH tunnel)
uv run python -m openadapt_ml.benchmarks.cli vm monitor

# 5. Cleanup (IMPORTANT: stops billing!)
uv run python -m openadapt_ml.benchmarks.cli vm delete -y
```

## What We DON'T Modify

- Task JSON files (154 tasks)
- Evaluator logic (getters, metrics)
- Navi agent code
- Flask server core endpoints
- WAA's run.py orchestration

## Using Custom Agents

The WAALiveAdapter in openadapt-evals allows running your own agents against
WAA without modifying WAA itself:

```python
from openadapt_evals.adapters.waa_live import WAALiveAdapter, WAALiveConfig

adapter = WAALiveAdapter(WAALiveConfig(
    server_url="http://vm-ip:5000",
    waa_examples_path="/path/to/WindowsAgentArena/evaluation_examples_windows"
))

# Your agent talks to WAA via HTTP
# WAA's evaluators determine success/failure
result = adapter.evaluate(task)
```

## ISO Download Options

### Option 1: Automatic (Recommended)

With `VERSION=11e` in run.sh, dockurr/windows automatically downloads
Windows 11 Enterprise Evaluation (~6GB) on first run. No user interaction needed.

### Option 2: Manual Download

If auto-download fails or you prefer manual:

1. Visit https://www.microsoft.com/en-us/evalcenter/download-windows-11-enterprise
2. Download the ISO (~6GB)
3. Place at: `WindowsAgentArena/src/win-arena-container/vm/image/setup.iso`
4. Run: `./run-local.sh --prepare-image true`

### Option 3: Fallback with Browser Prompt

The `scripts/waa_bootstrap_local.sh` script supports `--open-iso-page` flag
which opens the Microsoft download page in your browser if the ISO is missing.

## Troubleshooting

### Product Key Prompt

**Symptom**: Windows asks for a product key during installation.

**Fix**: Ensure `VERSION=11e` is set. Enterprise Evaluation has a built-in
volume license key (GVLK) that bypasses activation prompts.

### VNC Shows Black Screen

**Symptom**: VNC at localhost:8006 shows nothing.

**Fix**: Check QEMU is running: `docker exec winarena ps aux | grep qemu`
If QEMU is paused, restart the container.

### Docker Build Fails with "no space"

**Symptom**: Build fails with disk space errors.

**Fix**: Run `vm docker-move` to relocate Docker data to /mnt (147GB).

## References

- [Windows Agent Arena GitHub](https://github.com/microsoft/WindowsAgentArena)
- [WAA Paper](https://arxiv.org/abs/2409.08264)
- [dockurr/windows](https://github.com/dockur/windows) - Windows in Docker
