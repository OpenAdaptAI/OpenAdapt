#!/bin/bash
set -e
uv pip install -q -e ../capture -e ../types 2>&1 | tail -2
unset WAYLAND_DISPLAY DISPLAY
export XDG_SESSION_TYPE=x11 GDK_BACKEND=x11 NO_AT_BRIDGE=0 GTK_MODULES=atk-bridge
exec xvfb-run -a -s "-screen 0 1280x800x24" dbus-run-session -- \
  bash -c 'openbox >/dev/null 2>&1 & sleep 1; exec "$@"' bash \
  python benchmark/multi_surface_record/qualify_linux_cross_window.py --keep "$@"
