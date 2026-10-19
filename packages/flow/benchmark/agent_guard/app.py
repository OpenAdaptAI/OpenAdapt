"""MockMed's page in front of the effect test's record store.

The browser loads the MockMed single-page app (``openadapt_flow/mockmed/
static``). Its Save posts ``/api/encounter`` (the page's fault path, on with
``?fault=``), and the write lands in the SQLite system of record of
``benchmark/effect_e2e`` (encounters, plus the billing table no encounters
read covers), through that harness's own fault hook. So every
``effect_e2e`` fault class can be put under a real browser agent, and the
independent ground truth (:mod:`benchmark.effect_e2e.ground_truth`) reads the
file directly.

Two ways to inject a fault:

* **At the store** (``POST /api/fault {"fault": ...}``): every write gets that
  fault, whatever the page asked for. These are the ``effect_e2e`` classes,
  with their meaning there (``duplicate``: one request lands twice).
* **By the page** (``?fault=duplicate|optimistic|timeout`` with no store
  fault): MockMed's page sends the save twice, paints success before the
  server answers, or gives up after 1.2 s. Each request then lands once
  (``duplicate``), is refused with 409 (``optimistic``), or commits and
  answers late (``timeout``).

Routes: ``GET /api/records`` (the encounters read verify-run uses),
``GET /api/db`` (the same, in MockMed's shape), ``POST /api/reset``,
``POST /api/fault``, ``POST /api/encounter``, the ``/chart/`` pages, and the
static app. Localhost only; all data is synthetic.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

from benchmark.effect_e2e.record_service import TARGET_TYPE, _init_db, _Store
from openadapt_flow.mockmed.chart_pages import ChartPages
from openadapt_flow.mockmed.fault_server import STATIC_DIR

#: Page-delivered faults: what the store does with each request the page sends.
_PAGE_FAULT_AT_STORE = {"duplicate": "ok", "double": "ok", "idempotent": "ok"}


@dataclass
class App:
    base_url: str
    db_path: Path
    store: _Store
    stop: Callable[[], None]
    #: The fault every write gets, or None to follow the page's ``?fault=``.
    fault: Optional[str] = None
    writes: int = 0


def _handler(app_ref: dict, directory: str, chart: ChartPages):
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:  # noqa: A002
            pass

        def _json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the page gave up (the timeout fault)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}

        def do_GET(self) -> None:  # noqa: N802
            app: App = app_ref["app"]
            if chart.handle_get(self):
                return
            path = urlparse(self.path).path
            if path in ("/api/records", "/api/db"):
                self._json(200, {"records": app.store.read_encounters()})
                return
            super().do_GET()

        def do_POST(self) -> None:  # noqa: N802
            app: App = app_ref["app"]
            if chart.handle_post(self):
                return
            parsed = urlparse(self.path)
            body = self._body()
            if parsed.path == "/api/reset":
                app.store.reset(seed_concurrent=bool(body.get("seed_concurrent")))
                app.fault = body.get("fault") or None
                app.writes = 0
                self._json(200, {"ok": True})
                return
            if parsed.path == "/api/fault":
                app.fault = body.get("fault") or None
                self._json(200, {"ok": True, "fault": app.fault})
                return
            if parsed.path == "/api/encounter":
                page_fault = (parse_qs(parsed.query).get("fault", [""])[0]).strip()
                fault = app.fault
                if fault is None:
                    fault = _PAGE_FAULT_AT_STORE.get(page_fault, page_fault)
                app.writes += 1
                status = app.store.apply_write(
                    fault,
                    patient_id=str(body.get("patient_id", "")),
                    enc_type=str(body.get("type", "") or TARGET_TYPE),
                    note=str(body.get("note", "")),
                    key=body.get("key"),
                )
                self._json(status, {"ok": 200 <= status < 300})
                return
            self._json(404, {"ok": False})

    return partial(Handler, directory=directory)


def serve(db_path: Path, *, host: str = "127.0.0.1", port: int = 0) -> App:
    """Serve the app on a loopback port; the database is created fresh."""

    _init_db(db_path)
    store = _Store(db_path)
    chart = ChartPages(rows_for=store.rows_for_patient)
    ref: dict = {}
    httpd = ThreadingHTTPServer((host, port), _handler(ref, str(STATIC_DIR), chart))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def stop() -> None:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)

    app = App(
        base_url=f"http://{host}:{httpd.server_address[1]}",
        db_path=db_path,
        store=store,
        stop=stop,
    )
    ref["app"] = app
    return app
