#!/bin/bash
# Serve UI-Venus-1.5-8B as an OpenAI-compatible HTTP grounding endpoint.
#
# This serves the inclusionAI/UI-Venus-1.5-8B model via vLLM with an
# OpenAI-compatible chat completions API. Both the DemoExecutor and
# PlannerGrounderAgent can call this endpoint for click grounding.
#
# The model uses a native bounding-box prompt format:
#   "Outline the position corresponding to the instruction: <description>.
#    The output should be only [x1,y1,x2,y2]."
# and returns coordinates like [123, 456, 234, 567] which are parsed
# to center-click coordinates by _parse_bbox_to_action().
#
# Hardware requirements:
#   - GPU with >= 16GB VRAM (A10G 24GB recommended)
#   - ~16GB disk for model weights
#
# Prerequisites:
#   pip install vllm>=0.11.0 transformers>=4.57.0
#
# Usage:
#   # On a GPU machine (A10G 24GB, RTX 4090, L4, etc.):
#   bash scripts/serve_ui_venus.sh
#
#   # With custom port/host:
#   GROUNDER_PORT=8080 bash scripts/serve_ui_venus.sh
#
#   # With openadapt-gpu CLI (if available):
#   openadapt-gpu launch --model inclusionAI/UI-Venus-1.5-8B --engine vllm
#   # Then on the GPU instance:
#   bash scripts/serve_ui_venus.sh
#
# Connecting from DemoExecutor:
#   from openadapt_evals.agents.demo_executor import DemoExecutor
#   executor = DemoExecutor(
#       grounder_endpoint="http://gpu-host:8000",
#       grounder_model="gpt-4.1-mini",   # fallback, not used when endpoint is set
#   )
#
# Connecting from PlannerGrounderAgent:
#   agent = PlannerGrounderAgent(
#       planner="claude-sonnet-4-6",
#       grounder="http",
#       grounder_provider="http",
#       grounder_endpoint="http://gpu-host:8000",
#   )
#
# Connecting from the correction flywheel:
#   python scripts/run_correction_flywheel.py \
#       --task-config example_tasks/clear-browsing-data-chrome.yaml \
#       --grounder-endpoint http://gpu-host:8000 \
#       --demo-dir ./demos
#
# Verification (run from any machine that can reach the GPU host):
#   curl http://gpu-host:8000/v1/models
#   # Should list "UI-Venus-1.5-8B"
#
#   curl http://gpu-host:8000/v1/chat/completions \
#     -H "Content-Type: application/json" \
#     -d '{
#       "model": "UI-Venus-1.5-8B",
#       "messages": [{"role": "user", "content": "Hello"}],
#       "max_tokens": 32
#     }'
#   # Should return a chat completion response

set -euo pipefail

MODEL="${GROUNDER_MODEL:-inclusionAI/UI-Venus-1.5-8B}"
PORT="${GROUNDER_PORT:-8000}"
HOST="${GROUNDER_HOST:-0.0.0.0}"
MAX_MODEL_LEN="${GROUNDER_MAX_LEN:-4096}"
GPU_UTIL="${GROUNDER_GPU_UTIL:-0.9}"

echo "============================================"
echo "  UI-Venus-1.5-8B Grounding Server"
echo "============================================"
echo "  Model:           $MODEL"
echo "  Served as:       UI-Venus-1.5-8B"
echo "  Host:            $HOST"
echo "  Port:            $PORT"
echo "  Max seq length:  $MAX_MODEL_LEN"
echo "  GPU utilization: $GPU_UTIL"
echo ""
echo "  Endpoint:  http://$HOST:$PORT/v1/chat/completions"
echo "  Models:    http://$HOST:$PORT/v1/models"
echo "============================================"

# Check if vllm is installed
if ! command -v vllm &> /dev/null; then
    echo "ERROR: vllm is not installed."
    echo "Install with: pip install vllm>=0.11.0"
    exit 1
fi

# Check for GPU
if ! command -v nvidia-smi &> /dev/null; then
    echo "WARNING: nvidia-smi not found. vLLM requires a GPU."
    echo "If running on CPU, this will fail."
fi

echo ""
echo "Starting vLLM server..."
echo ""

exec vllm serve "$MODEL" \
    --served-model-name UI-Venus-1.5-8B \
    --host "$HOST" \
    --port "$PORT" \
    --tensor-parallel-size 1 \
    --trust-remote-code \
    --dtype float16 \
    --max-model-len "$MAX_MODEL_LEN" \
    --gpu-memory-utilization "$GPU_UTIL"
