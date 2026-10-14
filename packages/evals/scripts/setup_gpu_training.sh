#!/usr/bin/env bash
# Setup script for verl-agent training on a GPU VM.
#
# This runs ON the remote GPU VM (via SSH) to install:
#   - Miniconda + Python 3.12
#   - vLLM 0.11.0 (pulls PyTorch + CUDA)
#   - flash-attn
#   - verl-agent (from source)
#   - openadapt-evals (for WAADesktopEnv adapter)
#
# Prerequisites:
#   - Ubuntu 22.04 GPU VM with NVIDIA drivers installed
#   - nvidia-smi works
#
# Usage (run on the GPU VM):
#   bash setup_gpu_training.sh
#
# Or via SSH from local:
#   ssh user@gpu-vm 'bash -s' < scripts/setup_gpu_training.sh

set -euo pipefail

CONDA_ENV="verl-agent"
VLLM_VERSION="0.11.0"
FLASH_ATTN_VERSION="2.7.4.post1"
# VAGEN includes verl as a submodule + the GymImageEnv protocol, env registry,
# and GymAgentLoop that we integrate with.
VERL_AGENT_REPO="https://github.com/RAGEN-AI/VAGEN.git"
OPENADAPT_EVALS_REPO="https://github.com/OpenAdaptAI/openadapt-evals.git"
OPENADAPT_EVALS_BRANCH="${OPENADAPT_EVALS_BRANCH:-main}"

log() { echo "=== [$(date '+%H:%M:%S')] $*"; }

# --- Check GPU ---
log "Checking GPU availability..."
if ! command -v nvidia-smi &>/dev/null; then
    echo "ERROR: nvidia-smi not found. Install NVIDIA drivers first."
    exit 1
fi
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
GPU_COUNT=$(nvidia-smi --query-gpu=name --format=csv,noheader | wc -l)
log "Found $GPU_COUNT GPU(s)"

# --- Install Miniconda ---
if [ ! -d "$HOME/miniconda3" ]; then
    log "Installing Miniconda..."
    wget -q https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -O /tmp/miniconda.sh
    bash /tmp/miniconda.sh -b -p "$HOME/miniconda3"
    rm /tmp/miniconda.sh
else
    log "Miniconda already installed"
fi
eval "$("$HOME/miniconda3/bin/conda" shell.bash hook)"

# --- Accept conda TOS (required since Miniconda 2025) ---
log "Accepting conda Terms of Service..."
conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/main 2>/dev/null || true
conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/r 2>/dev/null || true

# --- Create conda env ---
if conda env list | grep -q "^${CONDA_ENV} "; then
    log "Conda env '$CONDA_ENV' exists, activating..."
else
    log "Creating conda env '$CONDA_ENV' with Python 3.12..."
    conda create -n "$CONDA_ENV" python=3.12 -y
fi
conda activate "$CONDA_ENV"

# --- Install vLLM (pulls PyTorch + CUDA) ---
log "Installing vLLM $VLLM_VERSION..."
pip3 install "vllm==$VLLM_VERSION"

# --- Install flash-attn (Ampere+ GPUs only) ---
# Flash Attention 2 requires compute capability >= 8.0 (Ampere: sm_80+).
# V100 (sm_70) and older GPUs are NOT supported — the build will fail or
# produce runtime errors.  We detect the GPU architecture via nvidia-smi
# and skip the install on unsupported hardware.
GPU_CC=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -1 | tr -d '.')
if [ "${GPU_CC:-0}" -ge "80" ]; then
    log "GPU compute capability ${GPU_CC} >= 80 — installing flash-attn $FLASH_ATTN_VERSION..."
    pip3 install "flash-attn==$FLASH_ATTN_VERSION" --no-build-isolation --no-cache-dir
else
    log "GPU compute capability ${GPU_CC:-unknown} < 80 — skipping flash-attn (requires Ampere+)"
fi

# --- Clone and install verl-agent ---
if [ ! -d "$HOME/verl-agent" ]; then
    log "Cloning VAGEN..."
    git clone --recurse-submodules "$VERL_AGENT_REPO" "$HOME/verl-agent"
else
    log "VAGEN already cloned, pulling latest..."
    cd "$HOME/verl-agent" && git pull && git submodule update --init --recursive
fi
log "Installing VAGEN..."
cd "$HOME/verl-agent"
pip install -e .

# --- Clone and install openadapt-evals (for WAADesktopEnv) ---
if [ ! -d "$HOME/openadapt-evals" ]; then
    log "Cloning openadapt-evals ($OPENADAPT_EVALS_BRANCH)..."
    git clone -b "$OPENADAPT_EVALS_BRANCH" "$OPENADAPT_EVALS_REPO" "$HOME/openadapt-evals"
else
    log "openadapt-evals already cloned, pulling latest..."
    cd "$HOME/openadapt-evals" && git pull
fi
log "Installing openadapt-evals..."
cd "$HOME/openadapt-evals"
pip install -e .

# --- Install wandb for logging ---
pip install wandb

# --- Verify installation ---
log "Verifying installation..."
python -c "import torch; print(f'PyTorch: {torch.__version__}, CUDA: {torch.cuda.is_available()}, GPUs: {torch.cuda.device_count()}')"
python -c "import vllm; print(f'vLLM: {vllm.__version__}')"
python -c "import ray; print(f'Ray: {ray.__version__}')"
python -c "from openadapt_evals.adapters.verl_env import WAADesktopEnv; print('WAADesktopEnv: OK')"

log "Setup complete! Activate with: conda activate $CONDA_ENV"
log ""
log "To start training, use the orchestration script:"
log "  python scripts/train_verl_e2e.py --gpu-ip \$(hostname -I | awk '{print \$1}') --task-id <WAA_UUID>"
log ""
log "Or via oa-vm CLI:"
log "  oa-vm gpu-train --gpu-ip \$(hostname -I | awk '{print \$1}') --task-id <WAA_UUID>"
log ""
log "GPU count: $GPU_COUNT"
