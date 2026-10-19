#!/bin/sh
# Run the harness with the repository's own packages on the path.
# APIGEN_PY: a Python with playwright and openadapt-flow's dependencies.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
FLOW=$(cd "$HERE/../.." && pwd)
cd "$FLOW"
PYTHONPATH="$FLOW/../types:$FLOW" exec "${APIGEN_PY:-python}" -m benchmark.api_generalization.harness "$@"
