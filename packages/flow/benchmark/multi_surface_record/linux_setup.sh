#!/usr/bin/env bash
# Inside the Linux qualification container: add what a cross-window recording
# needs on top of the image (FFmpeg for capture's video, Chromium for the
# browser surface, openadapt-capture), then run the given command.
set -euo pipefail
export PLAYWRIGHT_BROWSERS_PATH=/opt/venv/ms-playwright
apt-get update -qq >/dev/null
apt-get install -y -qq --no-install-recommends ffmpeg x11-xkb-utils >/dev/null
# Xvfb starts without a keyboard layout; without one, typed keys reach
# capture as bare keycodes.
setxkbmap us || true
# The venv volume is shared: always install this checkout, editable.
pip install -q --no-cache-dir -e "$REPO/packages/types" -e "$REPO/packages/flow[linux]" \
  -e "$REPO/packages/capture" playwright pytest python-xlib
python -m playwright install --with-deps chromium >/dev/null
exec "$@"
