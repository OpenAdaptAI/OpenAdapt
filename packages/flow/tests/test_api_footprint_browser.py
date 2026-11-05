"""Actual Chromium captures, JSON actuation and whole SQLite effects.

The synthetic editor has two existing inventory rows. A successful main
record readback cannot excuse changing the other row or omitting its count.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from openadapt_flow import network_capture as nc
from openadapt_flow.api_footprint import diff_dumps, footprint_verdict
from openadapt_flow.compiler.api_proposal import (
    ApiProposalError,
    admit_api_step,
    propose_api_step,
)
from openadapt_flow.ir import ActionKind, Step, Workflow
from openadapt_flow.runtime.actuators import ActuationStatus, session_headers
from openadapt_flow.runtime.actuators.web_form import (
    PlaywrightSessionTransport,
    WebFormActuator,
)
from openadapt_flow.runtime.effects import Effect, EffectKind, ValueExpr


class Editor:
    def __init__(self, mode):
        self.mode = mode
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.requests = []
        self.serial = 0
        self.reset(70)
        app = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, data, kind="application/json"):
                raw = data.encode()
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self.reply(
                    app.html() if self.path == "/editor" else "{}",
                    "text/html" if self.path == "/editor" else "application/json",
                )

            def mutate(self):
                body = json.loads(
                    self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}"
                )
                with app.lock:
                    app.requests.append(
                        {"method": self.command, "path": self.path, "body": body}
                    )
                    if self.path == "/records":
                        app.serial += 1
                        generated = mode == "generated_insert"
                        app.db.execute(
                            "INSERT INTO records VALUES (?,?,?)",
                            (
                                app.serial if generated else 1,
                                body["title"],
                                str(app.serial) if generated else "fixed",
                            ),
                        )
                        if not generated:
                            app.db.execute(
                                "UPDATE inventory SET state='saved', count=count+? WHERE id=?",
                                (body.get("increment", 1), body.get("target", 17)),
                            )
                    elif self.path == "/journal":
                        app.db.execute("INSERT INTO journal VALUES (1,'saved')")
                    elif self.path == "/pending":
                        app.db.execute("DELETE FROM pending WHERE id=1")
                    app.db.commit()
                self.reply('{"data":{"saved":true}}')

            do_POST = mutate
            do_DELETE = mutate

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def reset(self, count):
        with self.lock:
            self.db.executescript("""
                DROP TABLE IF EXISTS records; DROP TABLE IF EXISTS inventory;
                DROP TABLE IF EXISTS journal; DROP TABLE IF EXISTS pending;
                CREATE TABLE records(id INTEGER PRIMARY KEY, title TEXT, created TEXT);
                CREATE TABLE inventory(id INTEGER PRIMARY KEY, state TEXT, count INTEGER);
                CREATE TABLE journal(id INTEGER PRIMARY KEY, event TEXT);
                CREATE TABLE pending(id INTEGER PRIMARY KEY, state TEXT);
                INSERT INTO pending VALUES(1,'pending');
            """)
            self.db.executemany(
                "INSERT INTO inventory VALUES (?, 'ready', ?)",
                [(17, count), (29, count)],
            )
            self.db.commit()
            self.requests.clear()

    def dump(self):
        with self.lock:
            return {
                name: [
                    dict(r)
                    for r in self.db.execute(f"SELECT * FROM {name} ORDER BY id")
                ]
                for name in ("records", "inventory", "journal", "pending")
            }

    def html(self):
        return """<!doctype html><input id="title"><button id="save">Save</button><output id="done"></output>
<script>
const mode = MODE;
const headers = {'X-Fixture': 'synthetic-session', 'Content-Type': 'application/json'};
const send = async (path, body, method='POST') => {
  const r = await fetch(path, {method, headers, body:JSON.stringify(body)}); await r.json();
};
window.ready = (async () => {
  await fetch('/session', {headers});
  if (mode === 'load_write') await send('/loaded', {event:'load'});
})();
save.onclick = async () => {
  done.textContent = '';
  const initial = title.value.startsWith('Demonstrated');
  const body = {title:title.value};
  if (mode === 'wrong_identity') body.target = initial ? 29 : 17;
  if (mode === 'omitted_increment') body.increment = initial ? 0 : 1;
  await send('/records', body);
  if (mode === 'trailing_post') await send('/journal', {event:'saved'});
  if (mode === 'trailing_delete') await send('/pending', {}, 'DELETE');
  if (mode === 'trailing_query') await send('/graphql', {query:'query { health }'});
  done.textContent = 'saved';
};
</script>""".replace("MODE", json.dumps(self.mode))

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.db.close()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        yield browser
        browser.close()


def open_editor(app, page):
    page.goto(app.origin + "/editor")
    page.evaluate("window.ready")
    page.wait_for_load_state("networkidle")


def gui_save(page, title):
    page.fill("#title", title)
    page.click("#save")
    page.wait_for_function("done.textContent === 'saved'")
    page.wait_for_load_state("networkidle")


def capture_save(app, context, page, tmp_path):
    rec = tmp_path / "capture"
    rec.mkdir()
    capture = nc.NetworkCapture(context).start()
    try:
        open_editor(app, page)
        start = time.monotonic()
        stamps = [0.0]
        page.click("#title")
        stamps.append(time.monotonic() - start)
        page.fill("#title", "Demonstrated record")
        stamps.append(time.monotonic() - start)
        page.click("#save")
        page.wait_for_function("done.textContent === 'saved'")
        page.wait_for_load_state("networkidle")
        # A requestfinished callback reads headers/body before appending.
        # Drain those callbacks before detaching the capture listeners.
        expected = (
            4 if app.mode.startswith("trailing_") or app.mode == "load_write" else 3
        )
        deadline = time.monotonic() + 5
        while capture.count < expected and time.monotonic() < deadline:
            page.wait_for_timeout(20)
        assert capture.count >= expected, "Every fixture request must finish capture"
    finally:
        capture.stop()
    capture.write(rec, t0_monotonic=start)
    params = {"title": "Demonstrated record"}
    (rec / "meta.json").write_text(json.dumps({"params": params}))
    (rec / "events.jsonl").write_text(
        "".join(
            json.dumps(
                {"i": i, "kind": kind, "t": stamp, "url_before": app.origin + "/editor"}
            )
            + "\n"
            for i, (kind, stamp) in enumerate(zip(("click", "type", "click"), stamps))
        )
    )
    workflow = Workflow(
        name="synthetic-inventory-editor",
        params=params,
        steps=[
            Step(id="step_000", intent="focus", action=ActionKind.CLICK),
            Step(id="step_001", intent="type", action=ActionKind.TYPE, param="title"),
            Step(
                id="step_002",
                intent="save",
                action=ActionKind.CLICK,
                risk="irreversible",
                effects=[
                    Effect(
                        kind=EffectKind.RECORD_WRITTEN,
                        match={"title": ValueExpr(param="title")},
                        expected_count=1,
                    )
                ],
            ),
        ],
    )
    return workflow, rec


def retain(tmp_path, report):
    raw = json.dumps(report, indent=2, sort_keys=True)
    (tmp_path / "observations.json").write_text(raw)
    print("FOOTPRINT_OBSERVATION=" + json.dumps(report, sort_keys=True))
    root = os.environ.get("FOOTPRINT_EVIDENCE_DIR")
    if root:
        Path(root).mkdir(parents=True, exist_ok=True)
        (Path(root) / (report["mode"] + ".json")).write_text(raw)


@pytest.mark.parametrize(
    "mode",
    [
        "wrong_identity",
        "omitted_increment",
        "equivalent",
        "generated_insert",
        "trailing_post",
        "trailing_delete",
        "trailing_query",
        "load_write",
    ],
)
def test_actual_editor_capture_and_sqlite_footprint(mode, tmp_path, browser):
    app = Editor(mode)
    context = browser.new_context()
    session_headers.arm(context)
    page = context.new_page()
    report = {"mode": mode, "pairs": []}
    try:
        original, rec = capture_save(app, context, page, tmp_path)
        report["capture"] = nc.load(rec)
        report["captured_rows"] = app.dump()
        proposed, step, reasons = propose_api_step(original, rec)
        report.update(proposed=step is not None, reasons=reasons)
        if mode.startswith("trailing_") and mode != "trailing_query":
            save = next(r for r in report["capture"] if r["url"].endswith("/records"))
            path = "/journal" if mode == "trailing_post" else "/pending"
            trailing = next(r for r in report["capture"] if r["url"].endswith(path))
            assert trailing["t"] > save["t"]
            assert report["captured_rows"]["journal"] == (
                [{"id": 1, "event": "saved"}] if mode == "trailing_post" else []
            )
            assert report["captured_rows"]["pending"] == (
                [] if mode == "trailing_delete" else [{"id": 1, "state": "pending"}]
            )
            assert step is None and proposed == original, (
                "Captured trailing write was silently omitted"
            )
            assert path in " ".join(reasons)
            return
        assert step is not None, reasons
        temporary, _ = admit_api_step(
            proposed,
            admitted_by="synthetic-reviewer",
            identity={"subject_name": "title"},
        )
        binding = temporary.steps[-1].api_binding
        assert binding.admitted()
        gui, api, gm, am = [], [], [], []
        for index in range(3):
            pair = {}
            for arm in ("gui", "api"):
                marker = f"pair{index}{arm}unique"
                title = "Current record " + marker
                app.reset(70 + index * 3)
                open_editor(app, page)
                before = app.dump()
                if arm == "gui":
                    gui_save(page, title)
                    status = "browser-saved"
                else:
                    result = WebFormActuator(PlaywrightSessionTransport(page)).actuate(
                        binding, {"title": title}
                    )
                    status = result.status.value
                    assert result.status is ActuationStatus.ACTUATED, result.reason
                after = app.dump()
                assert (
                    len(after["records"]) == 1 and after["records"][0]["title"] == title
                )
                changes = diff_dumps(before, after)
                (gui if arm == "gui" else api).append(changes)
                (gm if arm == "gui" else am).append(marker)
                pair[arm] = {
                    "before": before,
                    "after": after,
                    "changes": changes,
                    "requests": list(app.requests),
                    "status": status,
                }
            report["pairs"].append(pair)
        verdict = footprint_verdict(gui, api, gm, am)
        report["verdict"] = verdict
        if verdict["equivalent"]:
            final, ids = admit_api_step(
                proposed,
                admitted_by="synthetic-reviewer",
                identity={"subject_name": "title"},
                footprint=verdict,
            )
            report["admitted"] = bool(ids) and final.steps[-1].api_binding.admitted()
        else:
            with pytest.raises(ApiProposalError, match="side effects"):
                admit_api_step(
                    proposed, admitted_by="synthetic-reviewer", footprint=verdict
                )
            report["admitted"] = False
        if mode == "wrong_identity":
            assert all(
                p["gui"]["after"]["inventory"][0]["state"] == "saved"
                and p["api"]["after"]["inventory"][0]["state"] == "ready"
                and p["api"]["after"]["inventory"][1]["state"] == "saved"
                for p in report["pairs"]
            )
            assert not verdict["equivalent"] and not report["admitted"]
        elif mode == "omitted_increment":
            assert all(
                p["gui"]["after"]["inventory"][0]["count"]
                == p["gui"]["before"]["inventory"][0]["count"] + 1
                and p["api"]["after"]["inventory"][0]["count"]
                == p["api"]["before"]["inventory"][0]["count"]
                for p in report["pairs"]
            )
            assert not verdict["equivalent"] and not report["admitted"]
        elif mode == "generated_insert":
            assert not verdict["equivalent"] and not report["admitted"]
        else:
            assert verdict["equivalent"] and report["admitted"], verdict
    finally:
        retain(tmp_path, report)
        context.close()
        app.close()
