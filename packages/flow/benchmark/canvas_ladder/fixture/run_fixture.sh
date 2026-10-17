#!/bin/bash
# Turnkey no-DOM canvas round-trip. Layout:
#   Xvnc :0        = the X display the kiosk runs on AND a VNC server on :5900
#   :5900          = TigerVNC framebuffer of the kiosk (the "remote session")
#   :6080          = noVNC/websockify -> renders :5900 into an HTML5 <canvas>
# The harness opens http://<host>:6080/vnc.html and drives the <canvas>.
set -e
export HOME=/root
export RDP_FIXTURE_SAVE_PATH="${RDP_FIXTURE_SAVE_PATH:-/opt/canvas_fixture/saved_note.txt}"
export RDP_FIXTURE_THEME="${RDP_FIXTURE_THEME:-light}"

log() { echo "[run_fixture] $*"; }

# Resolve the noVNC web root across Ubuntu package layouts.
NOVNC_WEB=/usr/share/novnc
[ -f "$NOVNC_WEB/vnc.html" ] || NOVNC_WEB=/usr/share/novnc/
# Some novnc packages ship vnc_lite.html only; symlink a stable entrypoint.
if [ ! -f "$NOVNC_WEB/vnc.html" ] && [ -f "$NOVNC_WEB/vnc_lite.html" ]; then
    ln -sf "$NOVNC_WEB/vnc_lite.html" "$NOVNC_WEB/vnc.html" || true
fi

log "starting Xvnc :0 (X display + VNC server on :5900, no auth, 1280x800x24)"
# -SecurityTypes None -> no VNC password (fixture is localhost-only in CI).
# Non-blinking apps + fixed geometry keep the framebuffer deterministic.
Xvnc :0 -geometry 1280x800 -depth 24 -rfbport 5900 \
    -SecurityTypes None -AlwaysShared -desktop canvas-fixture \
    >/tmp/xvnc.log 2>&1 &
sleep 3

log "launching kiosk on :0 (trial reset is in-process via SIGUSR1; see kiosk_app.py)"
DISPLAY=:0 python3 /opt/canvas_fixture/kiosk_app.py >/tmp/kiosk.log 2>&1 &
sleep 2

log "starting noVNC/websockify on :6080 (web root: $NOVNC_WEB) -> localhost:5900"
websockify --web="$NOVNC_WEB" 6080 localhost:5900 >/tmp/novnc.log 2>&1 &
sleep 3

log "fixture up: open http://<host>:6080/vnc.html?autoconnect=1&resize=off ; kiosk saves to $RDP_FIXTURE_SAVE_PATH"
# Keep the container alive.
tail -f /dev/null
