#!/bin/bash
# Turnkey real-RDP round-trip. Layout:
#   :0  = kiosk app (the "remote desktop")            -> served by shadow server
#   3389 = FreeRDP shadow server mirroring :0 over RDP
#   :1  = FreeRDP client rendering the RDP session fullscreen (what we observe)
set -euo pipefail
export HOME=/root
export RDP_FIXTURE_ORACLE_ROOT="${RDP_FIXTURE_ORACLE_ROOT:-/opt/rdp_fixture/oracle}"
export RDP_FIXTURE_SAVE_PATH="${RDP_FIXTURE_SAVE_PATH:-${RDP_FIXTURE_ORACLE_ROOT}/saved_note.txt}"
export RDP_FIXTURE_RESET_ACK_PATH="${RDP_FIXTURE_RESET_ACK_PATH:-${RDP_FIXTURE_ORACLE_ROOT}/reset_ack.txt}"
export RDP_FIXTURE_THEME="${RDP_FIXTURE_THEME:-light}"

mkdir -p "${RDP_FIXTURE_ORACLE_ROOT}"

log() { echo "[run_fixture] $*"; }

log "starting server display :0"
Xvfb :0 -screen 0 1280x800x24 -ac +extension DAMAGE +extension RANDR +extension XFIXES \
    >/tmp/xvfb0.log 2>&1 &
sleep 2
log "launching kiosk on :0 (trial reset is in-process via SIGUSR1; see kiosk_app.py)"
DISPLAY=:0 python3 /opt/rdp_fixture/kiosk_app.py >/tmp/kiosk.log 2>&1 &
sleep 2
log "starting FreeRDP shadow server on :3389 (mirrors :0)"
DISPLAY=:0 freerdp-shadow-cli3 /port:3389 /bind-address:0.0.0.0 -auth \
    >/tmp/shadow.log 2>&1 &
sleep 3
log "starting client display :1"
Xvfb :1 -screen 0 1280x800x24 -ac >/tmp/xvfb1.log 2>&1 &
sleep 2
# FreeRDP's X11 client needs normal focus/grab semantics to translate synthetic
# MotionNotify events into RDP pointer packets. Without a window manager,
# button events arrive but XTest pointer motion can be ignored, leaving clicks
# at the remote session's previous cursor location. Openbox is headless here;
# it manages only the isolated client display inside this container.
DISPLAY=:1 openbox >/tmp/openbox.log 2>&1 &
sleep 1
log "connecting FreeRDP client (fullscreen, LOSSLESS raw bitmaps) -> localhost:3389"
# Disable the RemoteFX / NSCodec / GFX-pipeline lossy codecs so the client
# decodes raw bitmap updates: frames are then pixel-DETERMINISTIC between the
# record and replay passes. Lossy codec jitter otherwise perturbs template
# scores and trips the identity band on write steps -- a real, but
# fixture-induced, over-halt. Vision/drift realism is injected in software on
# top of this clean baseline (see the harness _DriftBackend).
DISPLAY=:1 xfreerdp3 /v:127.0.0.1:3389 /u:ubuntu /p:ubuntu /size:1280x800 /f \
    -gfx -rfx -nsc /cert:ignore +auto-reconnect /log-level:ERROR \
    >/tmp/client.log 2>&1 &
sleep 4
log "fixture up: observe/inject on DISPLAY=:1; oracle root is $RDP_FIXTURE_ORACLE_ROOT"
# Keep the container alive.
tail -f /dev/null
