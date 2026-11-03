#!/usr/bin/env bash
# Run a command in the Linux qualification container, with the repository
# (and its git directory, so results record the commit) mounted at the same
# paths as on the host, inside its own X11 display and session D-Bus.
#
#   benchmark/linux_real_app/run.sh build
#   benchmark/linux_real_app/run.sh python benchmark/repair_every_backend/qualify_linux.py
#
# The image is oa-linux-qualify. The venv lives in the oa-linux-qualify-venv
# volume, so the editable install (OA_LINUX_SETUP=1, the first time) persists.
# Remove both when done:
#   podman rmi oa-linux-qualify; podman volume rm oa-linux-qualify-venv
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../../../.." && pwd)"
common="$(cd "$repo" && cd "$(git rev-parse --git-common-dir)" && pwd)"
engine="${CONTAINER_ENGINE:-podman}"
if [ "${1:-}" = build ]; then
  exec "$engine" build -t oa-linux-qualify -f "$here/Containerfile" "$here"
fi
exec "$engine" run --rm --memory 3g --cpus 2 \
  -v "$repo:$repo" -v "$common:$common:ro" -v oa-linux-qualify-venv:/opt/venv \
  -w "$repo/packages/flow" -e REPO="$repo" \
  -e OA_LINUX_SETUP="${OA_LINUX_SETUP:-0}" \
  oa-linux-qualify bash -lc '
    set -e
    git config --global --add safe.directory "*"
    if [ "$OA_LINUX_SETUP" = 1 ]; then
      pip install -q --no-cache-dir -e "$REPO/packages/types" -e "$REPO/packages/flow[linux]"
    fi
    exec dbus-run-session -- xvfb-run -a -s "-screen 0 1280x800x24" "$@"
  ' bash "$@"
