#!/usr/bin/env bash
# DRAFT. Artifact checks each package's standalone release workflow ran on its
# built wheel and sdist. Run from packages/<pkg> with the build in ./dist.
# Usage: package-artifact-gates.sh <pkg>
set -euo pipefail
pkg="${1:?package directory name}"
test -d dist || { echo "no dist/ in $(pwd)"; exit 1; }

case "$pkg" in
  types|privacy)
    python scripts/check_source_boundary.py --require-dist
    ;;
  capture)
    python scripts/verify_distribution.py dist/*
    python scripts/check_source_boundary.py --require-dist
    ;;
  agent)
    python scripts/check_release_artifacts.py dist
    python scripts/check_dist.py dist/*
    python scripts/check_source_boundary.py --require-dist
    python -m pip install --quiet "twine>=6.1" "packaging>=24.2"
    python -m twine check dist/*
    version="$(python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
    mcpb_dir="$(mktemp -d)"
    npx -y @anthropic-ai/mcpb@2.1.2 validate manifest.json
    npx -y @anthropic-ai/mcpb@2.1.2 pack . "$mcpb_dir/openadapt-agent-${version}.mcpb"
    python scripts/check_mcpb.py "$mcpb_dir"/*.mcpb
    ;;
  evals)
    python scripts/check_source_boundary.py --require-dist
    python scripts/check_third_party_notices.py --require-dist
    ;;
  flow)
    python scripts/check_release_consistency.py --require-dist
    ;;
  viewer)
    # The standalone release workflow ran no artifact check beyond the build.
    ;;
  *)
    echo "unknown package: $pkg"
    exit 1
    ;;
esac
echo "artifact gates passed for $pkg"
