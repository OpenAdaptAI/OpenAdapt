"""Does a Chrome the person started expose its page over macOS AX?

Run in the macOS guest's logged-in session after Chrome is open on the probe
page. Hit-tests a grid over Chrome's window with
AXUIElementCopyElementAtPosition (the recorder's point query) before and
after setting AXManualAccessibility on Chrome's application element, and
reports the roles and titles found.
"""

import json
import subprocess
import sys
import time

import ApplicationServices as AX
import Quartz


def chrome_pid():
    out = subprocess.run(
        ["pgrep", "-x", "Google Chrome"], capture_output=True, text=True
    )
    pids = [int(p) for p in out.stdout.split()]
    return min(pids) if pids else None


def window_bounds(pid):
    for w in Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
    ):
        if w.get("kCGWindowOwnerPID") == pid and "Probe page" in (
            w.get("kCGWindowName") or ""
        ):
            b = w["kCGWindowBounds"]
            return int(b["X"]), int(b["Y"]), int(b["Width"]), int(b["Height"])
    return None


def attr(el, name):
    err, value = AX.AXUIElementCopyAttributeValue(el, name, None)
    return value if err == 0 else None


def grid(pid, bounds):
    system = AX.AXUIElementCreateSystemWide()
    seen = {}
    x0, y0, w, h = bounds
    for y in range(y0 + 80, min(y0 + h, y0 + 400), 12):
        for x in range(x0 + 10, min(x0 + w, x0 + 700), 20):
            err, el = AX.AXUIElementCopyElementAtPosition(
                system, float(x), float(y), None
            )
            if err != 0 or el is None:
                continue
            role = attr(el, "AXRole")
            title = attr(el, "AXTitle") or attr(el, "AXDescription") or ""
            seen.setdefault(f"{role}|{str(title)[:40]}", [x, y])
    return seen


def main():
    pid = chrome_pid()
    bounds = window_bounds(pid) if pid else None
    result = {"pid": pid, "bounds": bounds}
    if not bounds:
        print(json.dumps(result))
        return 1
    app = AX.AXUIElementCreateApplication(pid)
    result["manual_before"] = str(attr(app, "AXManualAccessibility"))
    result["points_first"] = grid(pid, bounds)
    result["set_manual"] = AX.AXUIElementSetAttributeValue(
        app, "AXManualAccessibility", True
    )
    time.sleep(3)
    result["points_after_manual"] = grid(pid, bounds)
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
