#!/usr/bin/env python3
"""Window-scoped Discord recording; stop by pressing Escape."""
from __future__ import annotations

import threading
from pathlib import Path

from pynput import keyboard
from openadapt_flow.desktop_record import record_desktop_capture


def main() -> None:
    out = Path("app-run/rec")
    if out.exists():
        import shutil
        shutil.rmtree(out)

    stop_event = threading.Event()

    def on_press(key):
        if key == keyboard.Key.esc:
            print("\n[record] Escape pressed — stopping…")
            stop_event.set()
            return False
        return True

    listener = keyboard.Listener(on_press=on_press)
    listener.start()

    print("Recording Discord (window-scoped).")
    print("  Prefer: server icons, channel names, message box, Send/Enter.")
    print("  Avoid: clicking inside chat history / images.")
    print("  Press ESC when finished.")
    print()

    recording = record_desktop_capture(
        out,
        task_description="Discord stable workflow",
        window={"owner": "Discord", "title": None},
        stop=stop_event.is_set,
        announce=False,
    )
    listener.stop()
    print(f"Recording written to {recording}")


if __name__ == "__main__":
    main()
