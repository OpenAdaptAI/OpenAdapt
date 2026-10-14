#!/bin/bash
# Example: Generate a standalone benchmark viewer with embedded data

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
REPO_ROOT="$( cd "$SCRIPT_DIR/../.." && pwd )"

# Benchmark results live in the openadapt-ml checkout, which sits beside this
# one. Set $OPENADAPT_ML_DIR if yours is somewhere else.
ML_DIR="${OPENADAPT_ML_DIR:-$(dirname "$REPO_ROOT")/openadapt-ml}"
RESULTS_DIR="${BENCHMARK_RESULTS_DIR:-$ML_DIR/benchmark_results}"

# Check if benchmark_results exists
if [ ! -d "$RESULTS_DIR" ]; then
    echo "Error: benchmark_results directory not found at $RESULTS_DIR"
    echo "Set \$OPENADAPT_ML_DIR if your openadapt-ml checkout is elsewhere,"
    echo "or run a benchmark first:"
    echo "  cd \"$ML_DIR\""
    echo "  uv run python -m openadapt_ml.benchmarks.cli test-collection --tasks 5"
    exit 1
fi

# Find first run
RUN_NAME=$(ls -1 "$RESULTS_DIR" | grep -v ".json" | head -1)

if [ -z "$RUN_NAME" ]; then
    echo "Error: No benchmark runs found in $RESULTS_DIR"
    exit 1
fi

echo "Generating viewer for run: $RUN_NAME"

# Generate viewer
python "$SCRIPT_DIR/generator.py" \
    --results-dir "$RESULTS_DIR" \
    --run-name "$RUN_NAME" \
    --output /tmp/benchmark_viewer.html

echo ""
echo "✅ Generated: /tmp/benchmark_viewer.html"
echo ""
echo "Open in browser:"
echo "  open /tmp/benchmark_viewer.html"
