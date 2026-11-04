"""Check window-scoped input focus against real X11 windows.

Run by ``linux_xvfb_quickstart.sh`` inside its container, under Xvfb with
openbox and xdotool. This process opens three windows of its own: the
target, a second window, and a password dialog marked transient for the
target. All three share one process, as a browser's windows do. The check
pins the target with the real resolver, focuses each window with
``xdotool windowactivate`` (an EWMH request the window manager carries
out), and asks the real X11 probe and the scope whether input reached the
target.

It prints one JSON line of observations and exits non-zero on a mismatch.
"""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import time

import psutil
import xcffib
import xcffib.xproto as xproto

from openadapt_capture.window_capture import (
    FOCUS_EVIDENCE_WINDOW,
    INPUT_KEYBOARD_FOCUS_ELSEWHERE,
    INPUT_POINTER_OVER_OTHER_WINDOW,
    WindowCaptureScope,
    WindowInputNotOnTarget,
    WindowTarget,
)
from openadapt_capture.window_capture_linux import X11CompositeClient
from openadapt_capture.window_input_focus import X11InputFocusProbe


def main() -> int:
    conn = xcffib.connect(display=os.environ["DISPLAY"])
    screen = conn.get_setup().roots[conn.pref_screen]

    def atom(name: str) -> int:
        return int(conn.core.InternAtom(False, len(name), name).reply().atom)

    def set_property(window: int, name: int, kind: int, fmt: int, values: bytes) -> None:
        count = len(values) // (fmt // 8)
        conn.core.ChangeProperty(xproto.PropMode.Replace, window, name, kind, fmt, count, values)

    def open_window(title: str, x: int, y: int, *, transient_for: int | None = None) -> int:
        window = conn.generate_id()
        conn.core.CreateWindow(
            screen.root_depth,
            window,
            screen.root,
            x,
            y,
            400,
            300,
            0,
            xproto.WindowClass.InputOutput,
            screen.root_visual,
            xproto.CW.BackPixel,
            [screen.white_pixel],
        )
        encoded = title.encode()
        set_property(window, atom("_NET_WM_NAME"), atom("UTF8_STRING"), 8, encoded)
        set_property(window, xproto.Atom.WM_NAME, xproto.Atom.STRING, 8, encoded)
        set_property(
            window, atom("_NET_WM_PID"), xproto.Atom.CARDINAL, 32, struct.pack("=I", os.getpid())
        )
        if transient_for is not None:
            set_property(
                window,
                xproto.Atom.WM_TRANSIENT_FOR,
                xproto.Atom.WINDOW,
                32,
                struct.pack("=I", transient_for),
            )
            set_property(
                window,
                atom("_NET_WM_WINDOW_TYPE"),
                xproto.Atom.ATOM,
                32,
                struct.pack("=I", atom("_NET_WM_WINDOW_TYPE_DIALOG")),
            )
        conn.core.MapWindow(window)
        conn.flush()
        return window

    target_id = open_window("OA focus target", 40, 40)
    second_id = open_window("OA focus second", 140, 120)
    dialog_id = open_window("OA focus password", 200, 200, transient_for=target_id)
    time.sleep(1.0)
    # The window manager may place new windows where it likes. Overlap the
    # second window with the target so a click can land on it over the target.
    for window, x, y in ((target_id, 40, 40), (second_id, 140, 120)):
        subprocess.run(["xdotool", "windowmove", "--sync", str(window), str(x), str(y)], check=True)
    time.sleep(0.5)

    def activate(window: int) -> None:
        subprocess.run(["xdotool", "windowactivate", "--sync", str(window)], check=True)
        time.sleep(0.5)

    probe = X11InputFocusProbe()
    scope = WindowCaptureScope(
        WindowTarget(owner=psutil.Process().name(), title="OA focus target"),
        capturer=lambda _window: None,
        focus_probe=probe,
    )
    target = scope.resolve()
    observed: dict[str, object] = {
        "ids": {"target": target_id, "second": second_id, "dialog": dialog_id},
        "pinned": target.window_id,
    }
    failures: list[str] = []
    if target.window_id != target_id:
        failures.append("the resolver did not pin the target window")

    def refusal(**kwargs) -> str | None:
        try:
            scope.assert_input_on_target(target, **kwargs)
        except WindowInputNotOnTarget as exc:
            return exc.reason
        return None

    activate(target_id)
    focus = probe.keyboard_focus()
    observed["focus on target"] = vars(focus) if focus else None
    evidence = scope.assert_input_on_target(target)
    if focus is None or focus.window_id != target_id or evidence != FOCUS_EVIDENCE_WINDOW:
        failures.append("keys in the focused target were not accepted with window evidence")

    activate(second_id)
    focus = probe.keyboard_focus()
    observed["focus on second"] = vars(focus) if focus else None
    if focus is None or focus.window_id != second_id:
        failures.append("the probe did not name the second window")
    if refusal() != INPUT_KEYBOARD_FOCUS_ELSEWHERE:
        failures.append("keys in a second window of the same process were kept")
    # The second window is now on top. Click inside it where it covers the target.
    with X11CompositeClient() as client:
        record = client.window_record(second_id)
    if record is None:
        failures.append("the second window has no record")
        point = (0.0, 0.0)
    else:
        point = (float(record.bounds[0] + 20), float(record.bounds[1] + 20))
    tx, ty, tw, th = target.bounds
    observed["overlap point inside target"] = tx <= point[0] < tx + tw and ty <= point[1] < ty + th
    if not observed["overlap point inside target"]:
        failures.append("the second window does not overlap the target")
    owner = probe.window_at(*point)
    observed["window at overlap"] = vars(owner) if owner else None
    if refusal(point=point) != INPUT_POINTER_OVER_OTHER_WINDOW:
        failures.append("a click on the second window over the target was kept")

    activate(dialog_id)
    focus = probe.keyboard_focus()
    observed["focus on dialog"] = vars(focus) if focus else None
    if refusal() != INPUT_KEYBOARD_FOCUS_ELSEWHERE:
        failures.append("keys in the target's own password dialog were kept")

    print(json.dumps(observed, sort_keys=True))
    for failure in failures:
        print(f"focus check failed: {failure}", file=sys.stderr)
    conn.disconnect()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
