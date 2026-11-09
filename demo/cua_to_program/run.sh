#!/usr/bin/env bash
# Reproduce the CUA-to-program launch demo on openIMIS claim entry.
#
#   ANTHROPIC_API_KEY=... demo/cua_to_program/run.sh
#
# Needs: docker (linux/amd64 images; on Apple Silicon use Rosetta, e.g.
# `colima start --vm-type vz --vz-rosetta`), python3.12, uv, ffmpeg, ~10 GB of
# free disk. Spends about $0.15 on the Claude API (one computer-use run).
# All data is synthetic. The openIMIS compose file (AGPL) is fetched at run
# time and checked against pinned hashes; nothing from it is stored here.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
FLOW="$REPO/packages/flow"
RUN_ID="${RUN_ID:-$(date +%Y%m%d-%H%M)}"
TAG="${TAG:-CR}"            # claim-number prefix, unique per run (2-5 letters)
N="${N:-100}"
CHANGE_AT="${CHANGE_AT:-50}"

: "${ANTHROPIC_API_KEY:?set ANTHROPIC_API_KEY to your own key}"
avail_gb=$(df -g "$HOME" | awk 'NR==2 {print $4}')
if [ "${avail_gb:-0}" -lt 10 ]; then
  echo "need ~10 GB free disk (have ${avail_gb} GB)" >&2
  exit 1
fi

if [ ! -x "$FLOW/.venv/bin/python" ]; then
  uv venv -q --python 3.12 "$FLOW/.venv"
fi
PY="$FLOW/.venv/bin/python"
uv pip install -q --python "$PY" -e "$FLOW" anthropic pg8000 numpy pillow
"$PY" -m playwright install chromium >/dev/null

cd "$HERE"
echo "== openIMIS reference stack (pinned images, synthetic data)"
"$PY" "$FLOW/scripts/openimis_claims_demo.py" up
"$PY" pipeline.py bootstrap

echo "== 1. Claude does the claim once, recorded by the JIT wrapper"
"$PY" pipeline.py --cache "out/jit-cache-$RUN_ID" --run-id "$RUN_ID" --tag "$TAG" \
  record --agent claude --max-cost 4

echo "== 2. Admission attempt on the standard gate (expected to refuse today)"
"$PY" pipeline.py --cache "out/jit-cache-$RUN_ID" --run-id "$RUN_ID" --tag "$TAG" \
  admit || true
echo "== 2. Compile the recording into a program (demo profile)"
"$PY" pipeline.py --cache "out/jit-cache-$RUN_ID" --run-id "$RUN_ID" --tag "$TAG" compile

echo "== 3. The program runs the next $N claims; UI change at claim $CHANGE_AT"
"$PY" pipeline.py --cache "out/jit-cache-$RUN_ID" --run-id "$RUN_ID" --tag "$TAG" \
  batch --n "$N" --change-at "$CHANGE_AT" --max-attempts 3 \
  --pause-after-change-halt --video-records "1,2,3,$((CHANGE_AT - 1)),$CHANGE_AT"

"$PY" summarize.py --run-id "$RUN_ID"
"$PY" render.py --run-id "$RUN_ID" --cut full
"$PY" render.py --run-id "$RUN_ID" --cut social
echo "results: $HERE/results/$RUN_ID"
