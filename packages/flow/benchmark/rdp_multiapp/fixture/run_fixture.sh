#!/bin/bash
set -euo pipefail

export HOME=/root
export RDP_MULTIAPP_ORACLE_ROOT="${RDP_MULTIAPP_ORACLE_ROOT:-/opt/rdp_multiapp/oracle}"
mkdir -p "${RDP_MULTIAPP_ORACLE_ROOT}"

Xvfb :0 -screen 0 1280x800x24 -ac +extension DAMAGE +extension RANDR +extension XFIXES \
    >/tmp/xvfb0.log 2>&1 &
sleep 2
# Keep the remote display free of a second window manager. The fixture owns
# the X keyboard focus while the client-side Openbox session manages FreeRDP.
DISPLAY=:0 python3 /opt/rdp_multiapp/suite_app.py >/tmp/suite.log 2>&1 &
sleep 3
DISPLAY=:0 freerdp-shadow-cli3 /port:3389 /bind-address:0.0.0.0 -auth \
    >/tmp/shadow.log 2>&1 &
sleep 3

Xvfb :1 -screen 0 1280x800x24 -ac >/tmp/xvfb1.log 2>&1 &
sleep 2
DISPLAY=:1 openbox >/tmp/openbox1.log 2>&1 &
sleep 1
DISPLAY=:1 xfreerdp3 /v:127.0.0.1:3389 /u:ubuntu /p:ubuntu /size:1280x800 /f \
    -gfx -rfx -nsc /cert:ignore +auto-reconnect /log-level:ERROR \
    >/tmp/client.log 2>&1 &
sleep 4

tail -f /dev/null
