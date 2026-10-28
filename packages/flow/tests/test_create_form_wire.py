"""A create-new form's save is checked on the wire, not in the page.

The #1581 round-3 review found that page listeners which run after every
in-page check (a form submit listener, a document capture listener, a
formdata listener) can still change what a submission carries. So the
Playwright backend holds the save's request at the network and lets it go
only when its method, address and fields equal what the checked form would
send.

Real headless Chromium; the server records every request it receives, so
each case asserts what actually arrived.
"""

from __future__ import annotations

import http.server
import json
import threading
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import parse_qs, parse_qsl, urlsplit

import pytest

from openadapt_flow.compiler import compile_recording
from openadapt_flow.interactive_recorder import record_interactive
from openadapt_flow.ir import Workflow

sync_api = pytest.importorskip("playwright.sync_api")

pytestmark = pytest.mark.timeout(900)

_SCRIPTS = {
    # A form submit listener copies the app's current parent id in.
    "sub7": "window.currentPid = '7';",
    "sub9": "window.currentPid = '9';",
    # A document capture listener adds a record id: an edit-shaped submit.
    "bubble42": """
document.addEventListener('submit', (e) => {
  const h = document.createElement('input'); h.type = 'hidden'; h.name = 'id';
  h.value = '42'; e.target.appendChild(h);
}, true);""",
    # A formdata listener injects a record id into the entry list.
    "fd42": """
document.getElementById('ticket').addEventListener('formdata', (e) => {
  e.formData.set('id', '42');
});""",
    # An SPA: the submit listener sends the form as JSON with fetch.
    "fetch": """
document.getElementById('ticket').addEventListener('submit', (e) => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target, e.submitter).entries());
  fetch('/ticket', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(data)}).then(() => {
      document.body.innerHTML = '<h1>Ticket created</h1>';
    });
});""",
    "fetch42": """
document.getElementById('ticket').addEventListener('submit', (e) => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target, e.submitter).entries());
  data.id = '42';
  fetch('/ticket', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(data)}).then(() => {
      document.body.innerHTML = '<h1>Ticket created</h1>';
    });
});""",
}
_SUBMIT_COPIES = """
document.getElementById('ticket').addEventListener('submit', () => {
  document.querySelector('[name=pid]').value = window.currentPid;
}, true);"""


def _page(variant: str) -> str:
    pid = (
        '<input type="hidden" name="pid" value="">' if variant.startswith("sub") else ""
    )
    scripts = _SCRIPTS.get(variant, "") + (
        _SUBMIT_COPIES if variant.startswith("sub") else ""
    )
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>New ticket</title>
</head><body style="font:16px Arial;margin:24px"><h1>New ticket</h1>
<form id="ticket" method="post" action="/ticket">
  {pid}
  <p><label>Summary <input name="field_summary" size="40"></label></p>
  <p><label>Description <textarea name="field_description" rows="3"
     cols="40"></textarea></label></p>
  <p><input type="submit" name="submit" value="Create ticket"></p>
</form>
<script>{scripts}</script>
</body></html>"""


class _Server:
    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (http.server API)
                query = parse_qs(urlsplit(self.path).query)
                self._send(_page(query.get("v", ["plain"])[0]))

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode()
                if "json" in (self.headers.get("Content-Type") or ""):
                    fields = json.loads(body)
                else:
                    fields = dict(parse_qsl(body, keep_blank_values=True))
                server.received.append({"path": self.path, "fields": fields})
                self._send(
                    "<!doctype html><title>Created</title><h1>Ticket created</h1>"
                )

            def _send(self, html: str) -> None:
                body = html.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                pass

        self.http = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.http.server_address[1]}/newticket"


@pytest.fixture(scope="module")
def server() -> Iterator[_Server]:
    srv = _Server()
    yield srv
    srv.http.shutdown()


_BUNDLES: dict[str, Path] = {}


def _bundle(server: _Server, variant: str, root: Path) -> Path:
    if variant in _BUNDLES:
        return _BUNDLES[variant]
    out = root / variant

    def drive(page: Any, pump: Callable[[], Any]) -> None:
        def settle() -> None:
            for _ in range(12):
                pump()

        page.wait_for_selector("[name=field_summary]", state="visible")
        page.locator("[name=field_summary]").click()
        settle()
        page.keyboard.type("Printer jams on tray 2")
        settle()
        page.locator("[name=field_description]").click()
        settle()
        page.keyboard.type("Seen twice today")
        settle()
        page.locator("[name=submit]").click()
        settle()
        # The save navigates: let the recorder see the page it lands on.
        for _ in range(30):
            pump()

    rec = record_interactive(
        f"{server.url}?v={variant}",
        out / "rec",
        headless=True,
        script=drive,
        param_fields=("field_summary", "field_description"),
    )
    compile_recording(rec, out / "bundle", name="newticket")
    # A slow recording can leave the save unarmed; fail here, not later.
    assert Workflow.load(out / "bundle").steps[-1].anchor.form_identity is not None
    _BUNDLES[variant] = out / "bundle"
    return out / "bundle"


def _replay(
    server: _Server, variant: str, bundle: Path, run_dir: Path
) -> tuple[Any, list]:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.runtime import Replayer

    before = len(server.received)
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(f"{server.url}?v={variant}")
            page.wait_for_selector("[name=field_summary]", state="visible")
            report = Replayer(PlaywrightBackend(page)).run(
                Workflow.load(bundle),
                params={
                    "field_summary": "Scanner offline on floor 3",
                    "field_description": "Since this morning",
                },
                bundle_dir=bundle,
                run_dir=run_dir,
            )
            page.wait_for_timeout(500)
        finally:
            browser.close()
    return report, server.received[before:]


@pytest.mark.parametrize("variant", ["plain", "sub7", "fetch"])
def test_the_checked_submission_goes_out_once(
    server: _Server, tmp_path_factory: pytest.TempPathFactory, variant: str
) -> None:
    bundle = _bundle(server, variant, tmp_path_factory.mktemp("rec"))
    assert Workflow.load(bundle).steps[-1].anchor.form_identity is not None
    report, received = _replay(server, variant, bundle, tmp_path_factory.mktemp("run"))
    if variant == "sub7":
        # The submit listener fills the pid after the check, even on the
        # demonstrated page: what is sent differs from the checked form, so
        # it is held (a safe-direction false halt, documented).
        assert not report.success
        assert received == []
        return
    assert report.success, [(r.step_id, r.error) for r in report.results]
    assert len(received) == 1
    assert received[0]["fields"]["field_summary"] == "Scanner offline on floor 3"
    assert "id" not in received[0]["fields"]


@pytest.mark.parametrize(
    "recorded, replayed",
    [("sub7", "sub9"), ("plain", "bubble42"), ("plain", "fd42"), ("fetch", "fetch42")],
)
def test_a_submission_changed_after_the_check_never_leaves(
    server: _Server,
    tmp_path_factory: pytest.TempPathFactory,
    recorded: str,
    replayed: str,
) -> None:
    bundle = _bundle(server, recorded, tmp_path_factory.mktemp("rec"))
    report, received = _replay(server, replayed, bundle, tmp_path_factory.mktemp("run"))
    assert not report.success
    assert received == [], received
    errors = " ".join(r.error or "" for r in report.results)
    assert (
        "differs from the checked form" in errors
        or "changed after it was checked" in errors
    )
