#!/bin/bash
# Serve UI-Venus-1.5-8B as an OpenAI-compatible grounding endpoint.
#
# Prerequisites:
#   pip install vllm>=0.11.0 transformers>=4.57.0
#
# Usage:
#   # On a GPU machine (A10G 24GB, RTX 4090, etc.):
#   bash scripts/serve_grounder.sh
#
#   # Then from anywhere:
#   curl http://gpu-host:8000/v1/chat/completions \
#     -H "Content-Type: application/json" \
#     -d '{"model": "UI-Venus-1.5-8B", "messages": [...]}'
#
# The PlannerGrounderAgent connects to this endpoint:
#   agent = PlannerGrounderAgent(
#       planner="claude-sonnet-4-6",
#       grounder="http",
#       grounder_endpoint="http://gpu-host:8000/v1",
#   )

set -euo pipefail

MODEL="${GROUNDER_MODEL:-inclusionAI/UI-Venus-1.5-8B}"
PORT="${GROUNDER_PORT:-8000}"
HOST="${GROUNDER_HOST:-0.0.0.0}"
MAX_MODEL_LEN="${GROUNDER_MAX_LEN:-4096}"
GPU_UTIL="${GROUNDER_GPU_UTIL:-0.9}"

echo "Starting grounder server:"
echo "  Model: $MODEL"
echo "  Port: $PORT"
echo "  Max sequence length: $MAX_MODEL_LEN"
echo "  GPU memory utilization: $GPU_UTIL"

vllm serve "$MODEL" \
    --served-model-name UI-Venus-1.5-8B \
    --host "$HOST" \
    --port "$PORT" \
    --tensor-parallel-size 1 \
    --trust-remote-code \
    --dtype float16 \
    --max-model-len "$MAX_MODEL_LEN" \
    --gpu-memory-utilization "$GPU_UTIL"
