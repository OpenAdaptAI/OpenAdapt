#!/usr/bin/env bash
# Regenerate packages/flow/public-artifacts.json for the committed scoreboard
# data, from a clean checkout of HEAD.
#
# The release check walks the source tree whatever .gitignore says, so running
# its writer in a working tree with local fixture state would list generated
# files (fixture secrets, fetched compose files). Commit the data first, then:
#
#   packages/flow/benchmark/scoreboard/refresh_inventory.sh
#   git commit -m "flow: refresh the public artifact inventory" packages/flow/public-artifacts.json
set -euo pipefail
REPO="$(git rev-parse --show-toplevel)"
PY="${PY:-$REPO/packages/flow/.venv/bin/python}"
TMP="$(mktemp -d)"
trap 'git -C "$REPO" worktree remove "$TMP/wt" >/dev/null 2>&1 || true; rm -rf "$TMP"' EXIT
git -C "$REPO" worktree add -q --detach "$TMP/wt" HEAD
(cd "$TMP/wt/packages/flow" && "$PY" scripts/check_release_consistency.py --write-public-artifact-inventory >/dev/null)
cp "$TMP/wt/packages/flow/public-artifacts.json" "$REPO/packages/flow/public-artifacts.json"
git -C "$REPO" diff --stat -- packages/flow/public-artifacts.json
