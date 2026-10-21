"""Dump the AX facts the spike needs from one Chrome window (no secrets).

Prints roles, titles and descriptions, and the AXURL of the web area. Values
of secure text fields are never read.
"""

from __future__ import annotations

import sys

import Quartz
from ApplicationServices import (
    AXUIElementCopyAttributeValue,
    AXUIElementCreateApplication,
)


def attr(el, name):
    err, val = AXUIElementCopyAttributeValue(el, name, None)
    return val if err == 0 else None


def chrome_pid():
    for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionAll, 0):
        if w.get("kCGWindowOwnerName") == "Google Chrome":
            return int(w["kCGWindowOwnerPID"])
    raise SystemExit("Chrome not running")


def walk(el, depth=0, limit=12, out=None):
    role = attr(el, "AXRole")
    sub = attr(el, "AXSubrole")
    title = attr(el, "AXTitle")
    desc = attr(el, "AXDescription")
    line = f"{'  ' * depth}{role}/{sub} title={title!r} desc={desc!r}"
    if role == "AXWebArea":
        line += f" url={attr(el, 'AXURL')}"
    if role in ("AXTextField", "AXComboBox") and sub != "AXSecureTextField":
        val = attr(el, "AXValue")
        line += f" value={str(val)[:80]!r}"
    print(line)
    if depth >= limit:
        return
    for child in attr(el, "AXChildren") or []:
        walk(child, depth + 1, limit)


if __name__ == "__main__":
    title = sys.argv[1] if len(sys.argv) > 1 else "Spike Login"
    app = AXUIElementCreateApplication(chrome_pid())
    for win in attr(app, "AXWindows") or []:
        t = attr(win, "AXTitle") or ""
        if t.startswith(title):
            walk(win, limit=int(sys.argv[2]) if len(sys.argv) > 2 else 14)
