"""Record a browser-to-desktop-to-browser workflow in one session, on real apps.

Runs in the Linux qualification container (Xvfb, openbox, session D-Bus,
AT-SPI). A person's work is played as physical X input (XTest), never
through Flow:

1. a web page (stdlib HTTP server) in the browser that ``record --url``
   launches: "Start referral";
2. the GTK3 chart app (tests/native_apps/chart_gtk.py, SQLite): "New note",
   type the note, "Save note";
3. back to the browser: "Mark referral sent", which the server counts.

One ``openadapt-flow record --url ... --surface chart=linux:Chart`` session
records it while the windows change under it. The script then compiles the
recording and replays it through the CLI, and checks the effects in both
systems of record (the chart's SQLite rows, the server's count). It also
replays against the chart showing another patient and checks that nothing
is written there.

Usage::

    benchmark/linux_container/run.sh build
    OA_LINUX_SCREEN=2400x1000x24 benchmark/linux_container/run.sh bash \
        benchmark/multi_surface_record/linux_setup.sh \
        python benchmark/multi_surface_record/qualify_linux_cross_window.py

Writes ``benchmark/multi_surface_record/results/linux-cross-window-<date>.json``
(or ``--output``).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

FLOW = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
APP_SOURCE = FLOW / "tests" / "native_apps" / "chart_gtk.py"
TITLE = "Chart"
PATIENT = ("rc79284", "MRN RC79284 Okafor, Philip born 1966-02-17")
OTHER = ("rc48572", "MRN RC48572 Adeyemi, Grace born 1971-03-29")
NOTE = "follow up in two weeks"

PAGE = """<!doctype html>
<html><head><title>Referral desk</title>
<style>
body { font: 20px sans-serif; margin: 40px; }
button { font: 20px sans-serif; padding: 12px 24px; margin: 12px 0; display: block; }
</style></head>
<body>
<h1>Referral desk</h1>
<p id="state">Referral waiting</p>
<button id="start" onclick="go('start')">Start referral</button>
<button id="sent" onclick="go('sent')">Mark referral sent</button>
<script>
function go(what) {
  fetch('/' + what, {method: 'POST'}).then(() => {
    document.getElementById('state').textContent =
      what === 'start' ? 'Referral started' : 'Referral sent';
  });
}
function geo() {
  const ox = window.screenX + (window.outerWidth - window.innerWidth);
  const oy = window.screenY + (window.outerHeight - window.innerHeight);
  window.__oaGeo = [window.screenX, window.screenY, window.outerWidth,
                    window.innerWidth, window.outerHeight, window.innerHeight];
  const out = {};
  for (const id of ['start', 'sent']) {
    const r = document.getElementById(id).getBoundingClientRect();
    out[id] = [Math.round(ox + r.left + r.width / 2),
               Math.round(oy + r.top + r.height / 2)];
  }
  out.raw = window.__oaGeo;
  fetch('/geo', {method: 'POST', body: JSON.stringify(out)});
}
window.addEventListener('load', () => setTimeout(geo, 500));
</script>
</body></html>
"""


class Desk:
    """The web app and its system of record: counts of start and sent."""

    def __init__(self) -> None:
        self.counts = {"start": 0, "sent": 0}
        self.geo: dict[str, list[int]] = {}
        desk = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                body = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                data = self.rfile.read(length) if length else b""
                name = self.path.strip("/")
                if name == "geo":
                    desk.geo = json.loads(data or b"{}")
                elif name in desk.counts:
                    desk.counts[name] += 1
                self.send_response(204)
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"

    def wait_geo(self, timeout: float = 180.0) -> dict[str, list[int]]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.geo:
                return self.geo
            time.sleep(0.2)
        raise RuntimeError("the page never reported its button positions")


class Chart:
    def __init__(self, db: Path, patient: tuple[str, str]) -> None:
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("OPENADAPT_", "OAFLOW_"))
        }
        self.proc = subprocess.Popen(
            [sys.executable, str(APP_SOURCE), str(db), TITLE, *patient, "normal"],
            env=env,
        )

    def place(self, x: int, y: int, timeout: float = 20.0) -> None:
        """Put the chart window beside the browser, as a person arranges it."""
        from Xlib import X, display

        d = display.Display()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for w in d.screen().root.query_tree().children:
                for win in [w, *w.query_tree().children]:
                    try:
                        if win.get_wm_name() == TITLE:
                            top = w if w != win else win
                            top.configure(x=x, y=y)
                            d.sync()
                            return
                    except Exception:
                        continue
            time.sleep(0.3)
        raise RuntimeError("the chart window never appeared")

    def close(self) -> None:
        self.proc.kill()
        self.proc.wait(timeout=10)
        time.sleep(1.5)


def _rows(db: Path) -> list[tuple[str, str]]:
    if not db.exists():
        return []
    with sqlite3.connect(db) as conn:
        try:
            return list(conn.execute("SELECT patient_id, note FROM notes ORDER BY id"))
        except sqlite3.Error:
            return []


class Person:
    """Physical X input through XTest: what a person's hands would send."""

    def __init__(self) -> None:
        from Xlib import XK, X, display
        from Xlib.ext import xtest

        self.X, self.XK, self.xtest = X, XK, xtest
        self.d = display.Display()

    def click(self, x: int, y: int) -> None:
        X, d = self.X, self.d
        self.xtest.fake_input(d, X.MotionNotify, x=int(x), y=int(y))
        d.sync()
        time.sleep(0.3)
        self.xtest.fake_input(d, X.ButtonPress, 1)
        d.sync()
        time.sleep(0.08)
        self.xtest.fake_input(d, X.ButtonRelease, 1)
        d.sync()

    def type(self, text: str) -> None:
        X, d = self.X, self.d
        for ch in text:
            name = "space" if ch == " " else ch
            code = d.keysym_to_keycode(self.XK.string_to_keysym(name))
            self.xtest.fake_input(d, X.KeyPress, code)
            d.sync()
            time.sleep(0.04)
            self.xtest.fake_input(d, X.KeyRelease, code)
            d.sync()
            time.sleep(0.04)


def _chart_point(name: str, role: str, timeout: float = 20.0) -> tuple[int, int]:
    """Screen center of the chart window's element (AT-SPI, read only)."""

    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    def walk(node: Any, depth: int = 0) -> Optional[Any]:
        if depth > 30 or node is None:
            return None
        try:
            if node.get_name() == name and node.get_role_name() == role:
                return node
            for i in range(node.get_child_count()):
                found = walk(node.get_child_at_index(i), depth + 1)
                if found is not None:
                    return found
        except Exception:
            return None
        return None

    deadline = time.monotonic() + timeout
    seen: list[Any] = []
    while time.monotonic() < deadline:
        desktop = Atspi.get_desktop(0)
        for i in range(desktop.get_child_count()):
            app = desktop.get_child_at_index(i)
            if app is None or app.get_name() != "chart":
                continue
            node = walk(app)
            if node is not None:
                ext = node.get_extents(Atspi.CoordType.SCREEN)
                x, y = ext.x + ext.width // 2, ext.y + ext.height // 2
                seen.append([ext.x, ext.y, ext.width, ext.height])
                if ext.width > 0 and ext.height > 0 and 0 <= x < 4096 and 0 <= y < 4096:
                    return (x, y)
        time.sleep(0.3)
    raise RuntimeError(f"no on-screen {role} {name!r} in the chart window: {seen[-3:]}")


def _flow(*args: str, timeout: float = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "openadapt_flow", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(FLOW),
    )


def _tail(text: str, n: int = 25) -> list[str]:
    return text.strip().splitlines()[-n:]


def record(root: Path, desk: Desk, db: Path) -> dict[str, Any]:
    chart = Chart(db, PATIENT)
    chart.place(1400, 100)
    person = Person()
    done = root / "DONE"
    rec = root / "recording"
    log_path = root / "record.log"
    log = log_path.open("w")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "openadapt_flow",
            "record",
            "--url",
            desk.url,
            "--surface",
            "chart=linux:Chart",
            "--out",
            str(rec),
            "--until-cmd",
            f"test -f {done}",
        ],
        cwd=str(FLOW),
        # A file, never a pipe: capture logs enough to fill a pipe and block.
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    error = None
    out = ""
    try:
        geo = desk.wait_geo()
        time.sleep(3.0)
        person.click(*geo["start"])  # the browser
        time.sleep(2.5)
        person.click(*_chart_point("New note", "push button"))  # the chart
        time.sleep(1.5)
        person.click(*_chart_point("Note", "text"))
        time.sleep(0.8)
        person.type(NOTE)
        time.sleep(1.0)
        person.click(*_chart_point("Save note", "push button"))
        time.sleep(2.5)
        person.click(*geo["sent"])  # back to the browser
        time.sleep(3.0)
        done.touch()
        proc.wait(timeout=300)
    except Exception as exc:  # keep the recorder's own output as evidence
        error = f"{type(exc).__name__}: {exc}; page geometry {desk.geo}"
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=30)
        log.close()
        out = log_path.read_text(errors="replace")
        chart.close()
    events = []
    if (rec / "events.jsonl").exists():
        events = [
            json.loads(x) for x in (rec / "events.jsonl").read_text().splitlines()
        ]
    return {
        "exit": proc.returncode,
        "error": error,
        "output": _tail(out, 40),
        "events": [
            {k: e.get(k) for k in ("i", "kind", "surface", "x", "y", "viewport_before")}
            for e in events
        ],
        "recording": str(rec),
    }


def replay(
    root: Path, desk: Desk, bundle: Path, db: Path, patient: tuple[str, str], name: str
) -> dict[str, Any]:
    config = root / "deploy.yaml"
    config.write_text(
        "surfaces:\n"
        "  chart:\n"
        "    kind: linux\n"
        "    linux_app: chart\n"
        f"    linux_window_title: {TITLE}\n"
        "    linux_allow_physical_input: true\n"
    )
    chart = Chart(db, patient)
    chart.place(1400, 100)
    time.sleep(3.0)
    before_rows, before_counts = _rows(db), dict(desk.counts)
    run_dir = root / f"run-{name}"
    try:
        proc = _flow(
            "replay",
            str(bundle),
            "--url",
            desk.url,
            "--config",
            str(config),
            "--run-dir",
            str(run_dir),
        )
    finally:
        chart.close()
    report: dict[str, Any] = {}
    if (run_dir / "report.json").exists():
        report = json.loads((run_dir / "report.json").read_text())
    steps = report.get("steps") or report.get("step_results") or []
    return {
        "exit": proc.returncode,
        "output": _tail(proc.stdout + proc.stderr),
        "execution_outcome": report.get("execution_outcome"),
        "halt": report.get("halt_reason") or report.get("stop_reason"),
        "model_calls": report.get("model_calls"),
        "steps": [
            {k: s.get(k) for k in ("step_id", "action", "surface", "status", "error")}
            for s in steps
        ],
        "new_rows": _rows(db)[len(before_rows) :],
        "web_counts_delta": {k: desk.counts[k] - before_counts[k] for k in desk.counts},
    }


def qualify(root: Path) -> dict[str, Any]:
    desk = Desk()
    db = root / "chart.sqlite"
    result: dict[str, Any] = {"record": record(root, desk, db)}
    result["record"]["chart_rows"] = _rows(db)
    if result["record"]["exit"] != 0:
        return result
    bundle = root / "bundle"
    proc = _flow(
        "compile",
        result["record"]["recording"],
        "--out",
        str(bundle),
        "--name",
        "referral-cross-window",
    )
    result["compile"] = {
        "exit": proc.returncode,
        "output": _tail(proc.stdout + proc.stderr),
    }
    if proc.returncode != 0:
        return result
    workflow = json.loads((bundle / "workflow.json").read_text())
    result["compile"]["surfaces"] = workflow.get("surfaces")
    result["compile"]["steps"] = [
        {k: s.get(k) for k in ("id", "action", "surface", "expect")}
        for s in workflow.get("steps", [])
    ]
    result["replay_same_patient"] = replay(root, desk, bundle, db, PATIENT, "same")
    result["replay_other_patient"] = replay(root, desk, bundle, db, OTHER, "other")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="cross-window-"))
    started = datetime.now(timezone.utc)
    result = qualify(root)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=str(FLOW)
    ).stdout.strip()
    payload = {
        "started": started.isoformat(),
        "commit": commit,
        "platform": "linux x11 (Xvfb, openbox), AT-SPI, Chromium",
        "work_dir": str(root) if args.keep else None,
        **result,
    }
    out = args.output or (
        HERE / "results" / f"linux-cross-window-{started:%Y%m%d-%H%M%S}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, default=str))
    print(json.dumps(payload, indent=2, default=str)[:6000])
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
