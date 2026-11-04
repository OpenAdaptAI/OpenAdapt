"""Does a Chrome the person started themselves expose its page over AT-SPI?

Launches Chromium as an ordinary process (no automation, no flags), the way a
person opens it, on a local page with a labelled button and text field. Then
reads the AT-SPI tree from outside, in three states:

1. as launched;
2. after setting the session accessibility bus to enabled
   (org.a11y.Status IsEnabled / ScreenReaderEnabled), which is what an
   assistive tool does;
3. a fresh launch with the bus already enabled.

For each it reports whether the page's button and field appear with their
role and accessible name, and whether the address bar shows the URL.
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PAGE = b"""<!doctype html><title>Probe page</title>
<h1>Referral desk</h1>
<label for=note>Referral note</label><input id=note>
<button>Mark referral sent</button>"""


def serve() -> str:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:
            pass

        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(PAGE)

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}/"


def chromium() -> str:
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or os.path.expanduser(
        "~/.cache/ms-playwright"
    )
    hits = sorted(glob.glob(f"{root}/chromium-*/chrome-linux*/chrome"))
    if not hits:
        raise SystemExit("no Playwright Chromium found")
    return hits[-1]


def set_bus_enabled(value: bool) -> str:
    v = "true" if value else "false"
    out = []
    for prop in ("IsEnabled", "ScreenReaderEnabled"):
        r = subprocess.run(
            [
                "busctl",
                "--user",
                "set-property",
                "org.a11y.Bus",
                "/org/a11y/bus",
                "org.a11y.Status",
                prop,
                "b",
                v,
            ],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            r = subprocess.run(
                [
                    "gdbus",
                    "call",
                    "--session",
                    "--dest",
                    "org.a11y.Bus",
                    "--object-path",
                    "/org/a11y/bus",
                    "--method",
                    "org.freedesktop.DBus.Properties.Set",
                    "org.a11y.Status",
                    prop,
                    f"<{v}>",
                ],
                capture_output=True,
                text=True,
            )
        out.append(f"{prop}={r.returncode}:{(r.stderr or '').strip()[:80]}")
    return "; ".join(out)


def read_tree() -> dict[str, Any]:
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    found: dict[str, Any] = {"apps": [], "nodes": 0, "hits": []}
    wanted = {
        ("push button", "Mark referral sent"),
        ("button", "Mark referral sent"),
        ("entry", "Referral note"),
        ("text", "Referral note"),
    }

    def walk(node: Any, depth: int) -> None:
        if node is None or depth > 60 or found["nodes"] > 20000:
            return
        found["nodes"] += 1
        try:
            role, name = node.get_role_name(), node.get_name()
        except Exception:
            return
        if (role, name) in wanted:
            ext = node.get_extents(Atspi.CoordType.SCREEN)
            found["hits"].append([role, name, [ext.x, ext.y, ext.width, ext.height]])
        if role == "entry" and name and "Address" in name:
            try:
                text = node.get_text_iface()
                found["address"] = Atspi.Text.get_text(text, 0, -1) if text else None
            except Exception:
                found["address"] = "unreadable"
        try:
            for i in range(node.get_child_count()):
                walk(node.get_child_at_index(i), depth + 1)
        except Exception:
            return

    desktop = Atspi.get_desktop(0)
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if app is None:
            continue
        found["apps"].append(app.get_name())
        if "hrom" in (app.get_name() or ""):
            walk(app, 0)
    return found


def launch(url: str) -> subprocess.Popen:
    profile = tempfile.mkdtemp(prefix="probe-chrome-")
    return subprocess.Popen(
        [
            chromium(),
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--no-sandbox",
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main() -> int:
    url = serve()
    result: dict[str, Any] = {"chromium": chromium()}
    set_bus_enabled(False)
    proc = launch(url)
    time.sleep(8)
    result["1_as_launched"] = read_tree()
    result["bus_set"] = set_bus_enabled(True)
    time.sleep(5)
    result["2_after_bus_enabled"] = read_tree()
    proc.kill()
    proc.wait()
    time.sleep(2)
    proc = launch(url)
    time.sleep(8)
    result["3_launched_with_bus_enabled"] = read_tree()
    proc.kill()
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
