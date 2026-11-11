"""Real Chromium and GTK/AT-SPI against isolated synthetic SQLite effect stores.

No browser/vision/actuation mocks. The independent database and request log
are the oracle; run-report labels alone cannot make a test pass.
"""

from __future__ import annotations

import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from PIL import Image

from openadapt_flow.approval_mode.dry_run import DryRunGate
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    ApiBinding,
    Guard,
    Interstitial,
    Predicate,
    PredicateKind,
    Step,
    StructuralLocator,
    Workflow,
)
from openadapt_flow.runtime.effects import Effect, EffectKind
from openadapt_flow.runtime.replayer import Replayer
from tests.test_execution_profiles import _KEY

RECORD = "RC79284"
PAGE = b"""<!doctype html><html><body style="font:20px sans-serif">
<table><tr data-record-id="RC79284"><td id="record">Record RC79284</td>
<td><input id="note" aria-label="Note" value="prefilled"></td>
<td><button id="save">Save</button></td></tr></table>
<button id="navigate">Navigate</button><p>Ready</p>
<script>const write = operation => fetch('/write', {method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({operation, record_id:document.querySelector('#record').textContent, value:document.querySelector('#note').value})});
document.querySelector('#note').addEventListener('input',()=>write('autosave'));
document.querySelector('#save').onclick=()=>write('save');
document.querySelector('#navigate').onclick=()=>location.href='/navigate';
document.addEventListener('keydown',()=>write('key'));
</script></body></html>"""


def _rows(database):
    with sqlite3.connect(database) as connection:
        return connection.execute(
            "SELECT id, operation, record_id, value FROM effects ORDER BY id"
        ).fetchall()


def _effect():
    return Effect(
        kind=EffectKind.RECORD_WRITTEN, match={"record_id": RECORD}, expected_count=1
    )


@contextmanager
def _web(tmp_path):
    from playwright.sync_api import sync_playwright

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    requests = []
    database = tmp_path / "effects.sqlite3"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(["GET", self.path])
            if self.path == "/navigate":
                with sqlite3.connect(database) as connection:
                    connection.execute(
                        "INSERT INTO effects(operation, record_id, value) VALUES ('navigation', ?, 'prefilled')",
                        (RECORD,),
                    )
            self.send_response(200)
            self.end_headers()
            self.wfile.write(PAGE)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(["POST", self.path, body])
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "INSERT INTO effects(operation, record_id, value) VALUES (?, ?, ?)",
                    (body["operation"], body["record_id"], body["value"]),
                )
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as pw:
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                debug_port = listener.getsockname()[1]
            browser = pw.chromium.launch(
                headless=True, args=[f"--remote-debugging-port={debug_port}"]
            )
            try:
                page = browser.new_page(viewport={"width": 900, "height": 600})
                page.goto(f"http://127.0.0.1:{server.server_port}/")
                yield (
                    PlaywrightBackend(page),
                    requests,
                    f"http://127.0.0.1:{debug_port}",
                )
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
        (tmp_path / "requests.json").write_text(json.dumps(requests, indent=2))


@contextmanager
def _gtk(tmp_path):
    from openadapt_flow.backends.linux_backend import AtspiLinuxClient, LinuxBackend

    title = "Check It " + tmp_path.name
    fixture = Path(__file__).parent / "native_apps" / "check_it_gtk.py"
    log = (tmp_path / "gtk.log").open("w")
    process = subprocess.Popen(
        [
            sys.executable,
            str(fixture),
            str(tmp_path / "effects.sqlite3"),
            title,
            RECORD,
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    client = AtspiLinuxClient()
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            windows = client.find_windows("OpenAdapt Check It", title)
            if len(windows) == 1:
                client.focus_window(windows[0])
                if client.window_is_active(windows[0]):
                    break
            assert process.poll() is None, "GTK fixture exited before discovery"
            time.sleep(0.1)
        else:
            pytest.fail("owned GTK window did not become available")
        # Production physical input stays off: structural native input alone
        # is sufficient for the deliberate normal-save control.
        backend = LinuxBackend(client, app="OpenAdapt Check It", window_title=title)
        # Binding an AT-SPI window alone is insufficient: GTK must render on
        # this job's Xvfb, rather than an inherited Wayland display.
        frame = backend.screenshot()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with Image.open(io.BytesIO(frame)) as image:
                if any(low != high for low, high in image.convert("RGB").getextrema()):
                    break
            time.sleep(0.05)
            frame = backend.screenshot()
        else:
            pytest.fail("owned GTK window did not render nonblank Xvfb pixels")
        (tmp_path / "gtk-initial.png").write_bytes(frame)
        yield backend, [], None
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)
        log.close()
        (tmp_path / "process.json").write_text(
            json.dumps(
                {
                    "pid": process.pid,
                    "returncode": process.returncode,
                    "terminated": process.poll() is not None,
                }
            )
        )


def _anchor(backend, surface, name):
    locator = (
        StructuralLocator(
            selector={"Save": "#save", "Note": "#note", "Navigate": "#navigate"}[name]
        )
        if surface == "web"
        else StructuralLocator(
            role="textbox" if name == "Note" else "button", name=name
        )
    )
    handle = backend.locate_structural(locator)
    assert handle is not None
    x, y = handle.point
    text = backend.structured_text_at(x, y)
    if name == "Save":
        assert text, "real structured identity must be available"
    return Anchor(
        template="unused.png",
        structural=locator,
        region=(x - 3, y - 3, 6, 6),
        click_point=(x, y),
        ocr_text=name,
        structured_identity=text,
        structured_row=True,
    )


@pytest.mark.parametrize("surface", ["web", "gtk"])
@pytest.mark.parametrize(
    "case",
    [
        "prefilled",
        "read_only_prefix",
        "type",
        "click",
        "navigation",
        "wrong_identity",
        "guard",
        "interstitial",
        "api",
        "normal_save",
    ],
)
def test_no_input_check_against_real_application(tmp_path, monkeypatch, surface, case):
    if surface == "gtk" and (sys.platform != "linux" or not os.environ.get("DISPLAY")):
        pytest.skip("owned Xvfb/D-Bus Linux lane required")
    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", _KEY)
    database = tmp_path / "effects.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE effects(id INTEGER PRIMARY KEY, operation TEXT, record_id TEXT, value TEXT)"
        )
    context = _web if surface == "web" else _gtk
    with context(tmp_path) as (backend, requests, _endpoint):
        save = Step(
            id="save",
            intent="Save prefilled synthetic record",
            action=ActionKind.CLICK,
            anchor=_anchor(backend, surface, "Save"),
            identity_armed=True,
            risk="irreversible",
            effects=[_effect()],
            timeout_s=0.2,
        )
        steps = [save]
        if case == "read_only_prefix":
            steps.insert(
                0,
                Step(
                    id="observe", intent="Observe without input", action=ActionKind.WAIT
                ),
            )
        if case in {"type", "click", "navigation"}:
            name = (
                "Note"
                if case == "type"
                else ("Navigate" if case == "navigation" else "Save")
            )
            steps.insert(
                0,
                Step(
                    id="unknown-prefix",
                    intent="Unknown input",
                    action=ActionKind.TYPE if case == "type" else ActionKind.CLICK,
                    anchor=_anchor(backend, surface, name),
                    text="autosave" if case == "type" else None,
                    timeout_s=0.2,
                ),
            )
        if case == "wrong_identity":
            save.anchor.structured_identity = "Record RC48572"
        predicate = Predicate(
            kind=PredicateKind.PARAM_EQUALS,
            param="record",
            value="wrong" if case == "guard" else RECORD,
        )
        save.guard = Guard(predicate=predicate)
        save.wait_until = predicate
        if case == "api":
            save.api_binding = ApiBinding(
                url_template="/write",
                effects=[_effect()],
                on_unavailable="halt",
                body_template={
                    "operation": "api",
                    "record_id": RECORD,
                    "value": "prefilled",
                },
            )
        workflow = Workflow(name="Check-it synthetic " + surface, steps=steps)
        if case == "interstitial":
            workflow.interstitials = [
                Interstitial(
                    name="Known input overlay",
                    detect=Predicate(
                        kind=PredicateKind.TEXT_PRESENT, text="Ready", exact=True
                    ),
                    dismiss_key="Escape",
                    risk="reversible",
                    consequential=False,
                    clearance=Predicate(
                        kind=PredicateKind.TEXT_ABSENT, text="Ready", exact=True
                    ),
                )
            ]
        bundle = tmp_path / "bundle"
        workflow.save(bundle)
        before = _rows(database)
        assert before == []
        if case == "normal_save":
            # Deliberate positive delivery control; a real native/DOM invocation
            # must create one external effect. This is not dry-run evidence.
            handle = backend.locate_structural(save.anchor.structural)
            backend.act_structural(save.anchor.structural, handle)
            deadline = time.monotonic() + 5
            while not _rows(database) and time.monotonic() < deadline:
                time.sleep(0.05)
            assert len(_rows(database)) == 1
        else:
            from openadapt_flow.runtime.actuators import ApiActuator

            # A real HTTP actuator is present in the web bypass probe. Its
            # prepare/send path must remain entirely unreachable in dry mode.
            actuator = (
                ApiActuator(backend.page.url)
                if surface == "web" and case == "api"
                else None
            )
            engine = Replayer(
                backend,
                approval_gate=DryRunGate(),
                require_settled=True,
                api_actuator=actuator,
                poll_interval_s=0.05,
            )
            if case == "interstitial":
                assert engine._predicate_holds(
                    workflow.interstitials[0].detect,
                    backend.screenshot(),
                    bundle,
                    {"record": RECORD},
                    workflow=workflow,
                ), "the real fixture notice must activate the detector"
            report = engine.run(
                workflow,
                params={"record": RECORD},
                bundle_dir=bundle,
                run_dir=tmp_path / "run",
            )
            assert report.execution_outcome == "HALTED"
            assert report.transaction_outcome == (
                "REJECTED_POLICY"
                if case == "wrong_identity"
                else "HALTED_BEFORE_EFFECT"
            )
            if case == "wrong_identity":
                assert report.results[-1].identity.status == "mismatch"
            assert all(result.delivery_attempted is False for result in report.results)
            assert _rows(database) == before
            if case in {"prefilled", "read_only_prefix"}:
                assert report.approval.decision_reason == "dry_run"
                assert report.results[-1].identity.status == "verified"
                assert {c.detail for c in report.approval.pre_checks if c.passed} >= {
                    "guard_passed",
                    "wait_until_passed",
                    "stopped_before_delivery",
                }
            else:
                assert report.approval.decision_reason == "dry_run_refused"
            assert not any(
                row[0] == "POST" or row[1] == "/navigate" for row in requests
            )
        (tmp_path / "oracle.json").write_text(
            json.dumps(
                {
                    "before": before,
                    "after": _rows(database),
                    "case": case,
                    "surface": surface,
                },
                indent=2,
            )
        )


@pytest.mark.parametrize("sealed", [False, True])
def test_cli_explicit_dry_mode_on_an_already_open_chromium_page(
    tmp_path, monkeypatch, sealed
):
    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", _KEY)
    database = tmp_path / "effects.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE effects(id INTEGER PRIMARY KEY, operation TEXT, record_id TEXT, value TEXT)"
        )
    with _web(tmp_path) as (backend, requests, endpoint):
        workflow = Workflow(
            name="Attached Check-it dry-run",
            steps=[
                Step(
                    id="save",
                    intent="Save prefilled synthetic record",
                    action=ActionKind.CLICK,
                    anchor=_anchor(backend, "web", "Save"),
                    identity_armed=True,
                    risk="irreversible",
                    effects=[_effect()],
                )
            ],
        )
        bundle = tmp_path / "bundle"
        workflow.save(bundle)
        if sealed:
            from openadapt_flow.bundle_sealing import seal_bundle

            destination = tmp_path / "sealed-bundle"
            seal_bundle(bundle, destination)
            bundle = destination
        config = tmp_path / "deployment.json"
        config.write_text(
            json.dumps(
                {
                    "backend": {
                        "kind": "web",
                        "url": backend.page.url,
                        "browser_cdp_endpoint": endpoint,
                    }
                }
            )
        )
        # Desktop invokes the installed console entry point. Keep the original
        # plaintext module-entry control; exercise sealed input on that actual
        # Desktop path without changing the generic sealed-command allowlist.
        entrypoint = (
            [str(Path(sys.executable).with_name("openadapt-flow"))]
            if sealed
            else [sys.executable, "-m", "openadapt_flow"]
        )
        process = subprocess.run(
            [
                *entrypoint,
                "replay",
                str(bundle),
                "--stop-before-effect",
                "--config",
                str(config),
                "--run-dir",
                str(tmp_path / "run"),
            ],
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        (tmp_path / "cli.log").write_text(process.stdout + process.stderr)
        assert process.returncode != 0
        from openadapt_flow import sealed_set

        report = json.loads(sealed_set.read_bytes(tmp_path / "run" / "report.json"))
        if sealed:
            assert sealed_set.is_sealed(tmp_path / "run")
            sealed_set.SealedSet.open(tmp_path / "run", _KEY).verify_all()
            assert not (tmp_path / "run" / "report.json").exists()
            assert not list((tmp_path / "run").rglob("*.png"))
        assert report["execution_outcome"] == "HALTED"
        assert report["transaction_outcome"] == "HALTED_BEFORE_EFFECT"
        assert report["approval"]["decision_reason"] == "dry_run"
        assert all(
            result["delivery_attempted"] is False for result in report["results"]
        )
        assert _rows(database) == []
        assert not any(row[0] == "POST" or row[1] == "/navigate" for row in requests)
