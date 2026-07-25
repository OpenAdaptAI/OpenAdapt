#!/usr/bin/env bash
# Window-scoped PowerChart (Citrix Viewer) capture with:
#   - Esc to stop
#   - OPENADAPT_FLOW_SKIP_OOB=1 (drop out-of-window clicks instead of failing convert)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"
OUT_NAME="${1:-powerchart-rec}"
OUT_DIR="$ROOT/app-run/$OUT_NAME"

pkill -f "openadapt flow record --backend macos" 2>/dev/null || true
sleep 0.3
rm -rf "$OUT_DIR"

osascript <<'APPLESCRIPT' >/dev/null
tell application "System Events"
  tell process "Citrix Viewer" to set frontmost to true
end tell
APPLESCRIPT

export OPENADAPT_FLOW_SKIP_OOB=1
export OPENADAPT_FLOW_SKIP_UNSUPPORTED=1

python - <<'PY' &
import subprocess, sys
try:
    from pynput import keyboard
except Exception as e:
    print(f"ESC_WATCHER_UNAVAILABLE: {e}", flush=True)
    sys.exit(0)

def on_press(key):
    if key == keyboard.Key.esc:
        print("ESC detected — stopping recorder", flush=True)
        subprocess.run(
            ["pkill", "-INT", "-f", "openadapt flow record --backend macos"],
            check=False,
        )
        return False

print("ESC watcher armed — press Escape to stop", flush=True)
print("OPENADAPT_FLOW_SKIP_OOB=1 (out-of-window clicks will be skipped)", flush=True)
with keyboard.Listener(on_press=on_press) as listener:
    listener.join()
PY

cd "$ROOT/app-run"
exec openadapt flow record \
  --backend macos \
  --out "$OUT_NAME" \
  --window "Citrix Viewer" \
  --window-title "PowerChart" \
  --task "PowerChart click workflow"
