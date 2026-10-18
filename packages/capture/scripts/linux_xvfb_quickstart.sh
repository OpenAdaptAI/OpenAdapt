#!/usr/bin/env bash
# Run the README quick start against a real recorder on Linux, inside a
# throwaway container with an Xvfb display, X RECORD input from xdotool, and
# the distribution's FFmpeg. Hosted CI has no live Linux recording lane, so
# this is the reproducible check for the Linux limit stated in the README.
#
#   scripts/linux_xvfb_quickstart.sh    # uses a running docker, else podman
#
# It clicks into a terminal window, types "hello", stops the recorder the way
# Ctrl+C does, and exits non-zero unless the saved recording holds that click
# and that text. Nothing leaves the container; the recording is discarded with
# it.
set -euo pipefail

if [ "${1:-}" != "--inside" ]; then
  engine="${CONTAINER_ENGINE:-}"
  if [ -z "${engine}" ]; then
    for candidate in docker podman; do
      if command -v "${candidate}" >/dev/null && "${candidate}" info >/dev/null 2>&1; then
        engine="${candidate}"
        break
      fi
    done
  fi
  if [ -z "${engine}" ]; then
    echo "a running docker or podman is required (or set CONTAINER_ENGINE)" >&2
    exit 2
  fi
  root="$(cd "$(dirname "$0")/.." && pwd)"
  exec "${engine}" run --rm -v "${root}:/src:ro" \
    docker.io/library/python:3.12-slim-bookworm \
    bash /src/scripts/linux_xvfb_quickstart.sh --inside
fi

export DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  xvfb xkb-data x11-xkb-utils xdotool ffmpeg libxtst6 libx11-6 libxkbcommon0 \
  openbox xterm >/dev/null
python -m venv /venv
export PATH="/venv/bin:${PATH}"
mkdir -p /tmp/src
tar -C /src --exclude=.git --exclude=.venv -cf - . | tar -C /tmp/src -xf -
pip install -q /tmp/src

Xvfb :99 -screen 0 1280x800x24 >/tmp/xvfb.log 2>&1 &
export DISPLAY=:99
sleep 2
openbox >/dev/null 2>&1 &
sleep 1
# Typed text is recorded only when an application receives the keys, so type
# into a window under the click, as a person would.
xterm -geometry 80x24+0+0 >/dev/null 2>&1 &
sleep 1
cd /tmp

# Job control gives the recorder its own process group, as a terminal does.
# Without it, bash starts background jobs with SIGINT ignored.
set -m
capture record ./my-capture --description "Rename a file" >/tmp/record.log 2>&1 &
pid=$!
set +m
for _ in $(seq 1 60); do
  grep -q "Capture session:" /tmp/record.log && break
  kill -0 "${pid}" 2>/dev/null || break
  sleep 1
done

if grep -q "Capture session:" /tmp/record.log; then
  sleep 1
  xdotool mousemove 200 200 click 1
  sleep 0.5
  xdotool type --delay 80 "hello"
  sleep 1
  # Ctrl+C signals the whole foreground process group.
  kill -INT -- "-${pid}" 2>/dev/null || true
fi

status=0
# A recorder that stops making progress must not hold the container forever.
for _ in $(seq 1 60); do
  kill -0 "${pid}" 2>/dev/null || break
  sleep 1
done
if kill -0 "${pid}" 2>/dev/null; then
  echo "capture record did not exit within 60 seconds"
  kill -KILL -- "-${pid}" 2>/dev/null || true
  status=1
fi
wait "${pid}" 2>/dev/null || status=$?

grep -v "| DEBUG\|| INFO\|^ \|^$" /tmp/record.log | tail -15
if [ "${status}" -ne 0 ]; then
  echo "capture record failed (exit ${status})"
  exit "${status}"
fi
capture info ./my-capture
python - <<'EOF'
from openadapt_capture import CaptureSession
from openadapt_capture.events import EventType

with CaptureSession.load("./my-capture") as capture:
    actions = list(capture.actions())
    clicks = [
        action
        for action in actions
        if action.type == EventType.MOUSE_SINGLECLICK and (action.x, action.y) == (200, 200)
    ]
    text = "".join(
        action.text or "" for action in actions if action.type == EventType.KEY_TYPE
    )
print(f"click at (200, 200): {len(clicks)}; typed text: {text!r}")
if len(clicks) != 1 or text != "hello":
    raise SystemExit("the recording does not hold the click and the typed text")
EOF
