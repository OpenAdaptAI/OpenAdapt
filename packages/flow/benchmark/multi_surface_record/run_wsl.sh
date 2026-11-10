#!/bin/bash
# On the zenbook's WSL (remote-run.sh): run a cross-window script on an X11
# display (Xvfb + openbox) with a session D-Bus and AT-SPI.
#   run_wsl.sh [script.py] [args...]   (default: the cross-window qualification)
set -e
"${UV:-$HOME/.local/bin/uv}" pip install -q -e ../capture -e ../types python-xlib
script=${1:-benchmark/multi_surface_record/qualify_linux_cross_window.py}
shift || true
unset WAYLAND_DISPLAY DISPLAY
export XDG_SESSION_TYPE=x11 GDK_BACKEND=x11 NO_AT_BRIDGE=0 GTK_MODULES=atk-bridge
exec xvfb-run -a -s "-screen 0 1280x800x24" dbus-run-session -- \
  bash -c 'openbox >/dev/null 2>&1 & sleep 1; exec "$@"' bash \
  python "$script" "$@"
