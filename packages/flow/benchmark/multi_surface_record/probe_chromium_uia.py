"""Does an Edge or Chrome the person started expose its page over UI Automation?

Runs on the Windows desktop, in the person's session. Launches the browser as
an ordinary process (no automation, no flags) on a local page with a labelled
field and button, then reads the UIA tree from outside, twice: right away
(the first UIA client request is what asks Chromium to build its tree) and
after a short wait. Reports the page controls found with their control type
and name, and the address bar's value.

usage: python probe_chromium_uia.py OUT.json  (the browser is already open)
"""

import json
import sys
import time

import uiautomation as auto

PAGE = """<!doctype html><title>Probe page</title>
<h1>Referral desk</h1>
<label for=note>Referral note</label><input id=note>
<button>Mark referral sent</button>"""


def walk(root, out, depth=0):
    if depth > 60 or out["nodes"] > 20000:
        return
    out["nodes"] += 1
    try:
        kind, name = root.ControlTypeName, root.Name
    except Exception:
        return
    if (
        kind
        in (
            "DocumentControl",
            "ButtonControl",
            "EditControl",
            "TextControl",
            "GroupControl",
            "PaneControl",
        )
        and len(out.setdefault("seen", [])) < 120
    ):
        out["seen"].append([depth, kind, (name or "")[:40]])
    if (kind, name) in (
        ("ButtonControl", "Mark referral sent"),
        ("EditControl", "Referral note"),
    ):
        r = root.BoundingRectangle
        out["hits"].append([kind, name, [r.left, r.top, r.right, r.bottom]])
    if kind == "EditControl" and "address" in (name or "").lower():
        try:
            out["address"] = root.GetValuePattern().Value
        except Exception:
            out["address"] = "unreadable"
    try:
        for child in root.GetChildren():
            walk(child, out, depth + 1)
    except Exception:
        return


def read(title_part):
    out = {"nodes": 0, "hits": [], "windows": []}
    for w in auto.GetRootControl().GetChildren():
        out["windows"].append([w.Name[:60], w.ClassName])
        if (
            title_part in (w.Name or "")
            or "Edge" in (w.Name or "")
            or "Chrome" in (w.Name or "")
        ):
            walk(w, out)
    return out


def _windows(title_part):
    import ctypes
    import ctypes.wintypes as wt

    user32 = ctypes.windll.user32
    found = {"top": [], "render": []}
    proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def child_cb(hwnd, _):
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if cls.value.startswith("Chrome_RenderWidgetHost"):
            found["render"].append(hwnd)
        return True

    child_proc = proc(child_cb)

    def top_cb(hwnd, _):
        n = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, n, 256)
        if title_part in n.value:
            found["top"].append(hwnd)
            user32.EnumChildWindows(hwnd, child_proc, 0)
        return True

    user32.EnumWindows(proc(top_cb), 0)
    return found


def msaa_request(hwnds):
    """AccessibleObjectFromWindow(OBJID_CLIENT) and read its child count."""
    import ctypes

    import comtypes.client  # noqa: F401
    from comtypes import POINTER
    from comtypes.automation import VARIANT  # noqa: F401

    out = []
    try:
        from comtypes.gen.Accessibility import IAccessible
    except ImportError:
        comtypes.client.GetModule("oleacc.dll")
        from comtypes.gen.Accessibility import IAccessible
    for hwnd in hwnds:
        ptr = POINTER(IAccessible)()
        rc = (
            ctypes.oledll.oleacc.AccessibleObjectFromWindow(
                hwnd, -4, ctypes.byref(IAccessible._iid_), ctypes.byref(ptr)
            )
            if False
            else ctypes.windll.oleacc.AccessibleObjectFromWindow(
                hwnd, -4, ctypes.byref(IAccessible._iid_), ctypes.byref(ptr)
            )
        )
        count = None
        try:
            count = ptr.accChildCount
        except Exception as exc:
            count = repr(exc)[:80]
        out.append([int(hwnd or 0), rc, count])
    return out


def ia2_request(hwnds):
    """QueryService for IAccessible2, as NVDA and JAWS do."""
    import ctypes

    import comtypes
    from comtypes import GUID, POINTER, IUnknown
    from comtypes.gen.Accessibility import IAccessible

    class IServiceProvider(IUnknown):
        _iid_ = GUID("{6D5140C1-7436-11CE-8034-00AA006009FA}")
        _methods_ = [
            comtypes.COMMETHOD(
                [],
                comtypes.HRESULT,
                "QueryService",
                (["in"], POINTER(GUID), "guidService"),
                (["in"], POINTER(GUID), "riid"),
                (["out"], POINTER(POINTER(IUnknown)), "ppvObject"),
            )
        ]

    ia2 = GUID("{E89F726E-C4F4-4c19-BB19-B647D7FA8478}")
    out = []
    for hwnd in hwnds:
        ptr = POINTER(IAccessible)()
        ctypes.windll.oleacc.AccessibleObjectFromWindow(
            hwnd, -4, ctypes.byref(IAccessible._iid_), ctypes.byref(ptr)
        )
        try:
            sp = ptr.QueryInterface(IServiceProvider)
            got = sp.QueryService(ctypes.byref(IAccessible._iid_), ctypes.byref(ia2))
            out.append([int(hwnd), "ok", bool(got)])
        except Exception as exc:
            out.append([int(hwnd), "error", repr(exc)[:120]])
    return out


def screen_reader_flag(on):
    import ctypes

    SPI_SETSCREENREADER, SPIF_SENDCHANGE = 0x0047, 0x0002
    return ctypes.windll.user32.SystemParametersInfoW(
        SPI_SETSCREENREADER, int(on), None, SPIF_SENDCHANGE
    )


def point_grid(title_part):
    """UIA ElementFromPoint over the page area: the recorder's own query."""
    import ctypes
    import ctypes.wintypes as wt

    w = _windows(title_part)["top"]
    if not w:
        return {"window": False}
    r = wt.RECT()
    ctypes.windll.user32.GetWindowRect(w[0], ctypes.byref(r))
    seen = {}
    for y in range(r.top + 120, min(r.bottom, r.top + 420), 12):
        for x in range(r.left + 10, min(r.right, r.left + 700), 20):
            try:
                c = auto.ControlFromPoint(x, y)
                key = f"{c.ControlTypeName}|{c.Name[:40]}"
            except Exception as exc:
                key = "error|" + repr(exc)[:40]
            seen.setdefault(key, [x, y])
    return {
        "window": True,
        "rect": [r.left, r.top, r.right, r.bottom],
        "elements": seen,
    }


def main():
    if sys.argv[1] == "--screen-reader-flag":
        screen_reader_flag(sys.argv[2] == "on")
        return
    target = sys.argv[1]
    result = {"points_first": point_grid("Probe page")}
    time.sleep(3)
    result["points_after_3s"] = point_grid("Probe page")
    json.dump(result, open(target, "w"), indent=1, default=str)


if __name__ == "__main__":
    main()
