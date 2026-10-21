"""Real-browser cases the fakes do not model (docs/audits/REALISM_AUDIT.md).

Each test records a short task on a local web app in headless Chromium,
compiles it, then replays it after the page changes in a way real apps do:

* the field the task types into already holds text (form restore, autofill,
  a search box that keeps its last query);
* the app raises a ``confirm()`` the recording never saw;
* two worklist rows show the same visible text and swap order;
* the page retitles on every navigation.

Every replay must end in one of two ways: the run halts, or it did exactly
what was recorded. A run that reports success after doing something else is
the failure these tests exist to catch.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("playwright.sync_api")

from openadapt_flow.backends.playwright_backend import PlaywrightBackend  # noqa: E402
from openadapt_flow.compiler import compile_recording  # noqa: E402
from openadapt_flow.ir import Workflow  # noqa: E402
from openadapt_flow.recorder import Recorder  # noqa: E402
from openadapt_flow.runtime.replayer import Replayer  # noqa: E402

pytestmark = pytest.mark.timeout(600)

VIEWPORT = {"width": 900, "height": 600}

_STYLE = """
body { font-family: sans-serif; margin: 24px; font-size: 18px; }
input, button { font-size: 18px; padding: 6px; margin: 4px; }
li { padding: 10px; border: 1px solid #888; margin: 6px; list-style: none;
     cursor: pointer; width: 420px; }
#status { margin-top: 16px; font-weight: bold; }
"""


def _form(query: dict[str, list[str]]) -> str:
    prefill = query.get("prefill", [""])[0]
    ask = "confirm" in query
    return f"""<!doctype html><html><head><title>New note</title>
<style>{_STYLE}</style></head><body>
<h1>New note</h1>
<p><label for="name">Patient</label> <input id="name" autocomplete="off"></p>
<p style="margin-top:120px">Clinic intake form, draft copy
<button id="save">Save note</button></p>
<div id="status">Not saved</div>
<script>
const prefill = {prefill!r};
// Autofill (or an app restoring the last value) writes the field when it
// gains focus, after the click that the recording saw on an empty field.
const field = document.getElementById('name');
field.addEventListener('focus', () => {{
  if (prefill && !field.dataset.filled) {{ field.dataset.filled = '1'; field.value = prefill; }}
}});
document.getElementById('save').onclick = () => {{
  if ({str(ask).lower()} && !confirm('Discard the draft on the other tab?')) return;
  const v = document.getElementById('name').value;
  document.getElementById('status').textContent = 'Saved ' + v;
  document.title = 'Saved ' + v;
}};
</script></body></html>"""


def _worklist(query: dict[str, list[str]]) -> str:
    order = query.get("order", ["ab"])[0]
    rows = {
        "a": ("1001", "John Smith Follow-up Dr Lee"),
        "b": ("1002", "John Smith Follow-up Dr Lee"),
        "c": ("1003", "Maria Garcia Intake Dr Lee"),
    }
    items = "".join(
        f'<li data-id="{rows[k][0]}" onclick="openRow(this)">{rows[k][1]}</li>'
        for k in order + "c"
    )
    return f"""<!doctype html><html><head><title>Worklist</title>
<style>{_STYLE}</style></head><body>
<h1>Worklist</h1><ul>{items}</ul><div id="status">No chart open</div>
<div id="chart" style="display:none; margin-top:24px">
  <p>Chart header: John Smith, follow-up visit</p>
  <button id="sign">Sign visit note</button>
</div>
<div id="signed">Nothing signed</div>
<script>
let open = null;
function openRow(li) {{
  open = li.dataset.id;
  document.getElementById('status').textContent = 'Opened chart ' + open;
  document.getElementById('chart').style.display = 'block';
}}
document.getElementById('sign').onclick = () => {{
  document.getElementById('signed').textContent = 'Signed chart ' + open;
}};
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        page = {"/form.html": _form, "/worklist.html": _worklist}.get(parsed.path)
        if page is None:
            self.send_error(404)
            return
        body = page(query).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture(scope="module")
def app_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(scope="module")
def browser() -> Iterator[object]:
    from openadapt_flow._browser_setup import ensure_chromium_installed

    ensure_chromium_installed()
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        chromium = pw.chromium.launch(headless=True)
        yield chromium
        chromium.close()


def _page(browser: object, url: str):
    page = browser.new_page(viewport=VIEWPORT, device_scale_factor=1)  # type: ignore[attr-defined]
    page.goto(url)
    page.wait_for_load_state("load")
    page.wait_for_timeout(150)
    return page


def _center(page, selector: str) -> tuple[int, int]:
    box = page.locator(selector).first.bounding_box()
    assert box is not None, selector
    return int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)


def _recorder(page, root: Path, url: str) -> Recorder:
    return Recorder(
        PlaywrightBackend(page),
        root / "recording",
        app_url=url,
        settle_interval_s=0.05,
        settle_timeout_s=2.0,
    )


def _replay(browser, url, bundle_dir: Path, run_dir: Path, params=None):
    page = _page(browser, url)
    report = Replayer(PlaywrightBackend(page), poll_interval_s=0.05).run(
        Workflow.load(bundle_dir),
        params=params or {},
        bundle_dir=bundle_dir,
        run_dir=run_dir,
    )
    return report, page


def _summary(report) -> str:
    return "\n".join(f"{r.step_id} ok={r.ok} err={r.error}" for r in report.results)


@pytest.fixture(scope="module")
def note_bundle(app_url, browser, tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("note")
    url = app_url + "/form.html"
    page = _page(browser, url)
    try:
        recorder = _recorder(page, root, url)
        recorder.click(*_center(page, "#name"))
        recorder.type_text("Jane Doe", param="patient")
        recorder.click(*_center(page, "#save"))
        assert page.locator("#status").inner_text() == "Saved Jane Doe"
        recording = recorder.finish()
    finally:
        page.close()
    compile_recording(recording, root / "bundle", name="note")
    return root / "bundle"


def test_note_replays_on_the_page_it_was_recorded_on(
    note_bundle, app_url, browser, tmp_path
) -> None:
    """Control: an unchanged page with a new parameter saves the new value."""

    report, page = _replay(
        browser,
        app_url + "/form.html",
        note_bundle,
        tmp_path / "run",
        params={"patient": "Ada"},
    )
    try:
        assert report.success, _summary(report)
        assert page.locator("#status").inner_text() == "Saved Ada"
    finally:
        page.close()


def test_a_field_autofilled_on_focus_never_saves_a_concatenation(
    note_bundle, app_url, browser, tmp_path
) -> None:
    """The field fills itself when focused (autofill, a restored draft).

    Typing appends. A run that reports success with "John SmithAda" saved
    has written a wrong value silently. Today the click's region check halts
    the run before typing; this pins that.
    """

    report, page = _replay(
        browser,
        app_url + "/form.html?prefill=John%20Smith",
        note_bundle,
        tmp_path / "run",
        params={"patient": "Ada"},
    )
    try:
        status = page.locator("#status").inner_text()
        if report.success:
            assert status == "Saved Ada", _summary(report)
        else:
            assert status in {"Not saved", "Saved Ada"}, status
    finally:
        page.close()


def test_an_unrecorded_confirm_dialog_never_ends_in_success(
    note_bundle, app_url, browser, tmp_path
) -> None:
    """Save raises a ``confirm()`` the recording never saw.

    The browser dismisses it, so nothing is saved. The run must not report
    success on a save that did not happen.
    """

    report, page = _replay(
        browser,
        app_url + "/form.html?confirm=1",
        note_bundle,
        tmp_path / "run",
        params={"patient": "Ada"},
    )
    try:
        status = page.locator("#status").inner_text()
        assert not (report.success and status == "Not saved"), _summary(report)
    finally:
        page.close()


@pytest.fixture(scope="module")
def worklist_bundle(app_url, browser, tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("worklist")
    url = app_url + "/worklist.html"
    page = _page(browser, url)
    try:
        recorder = _recorder(page, root, url)
        recorder.click(*_center(page, "li[data-id='1001']"))
        assert page.locator("#status").inner_text() == "Opened chart 1001"
        recorder.click(*_center(page, "#sign"))
        assert page.locator("#signed").inner_text() == "Signed chart 1001"
        recording = recorder.finish()
    finally:
        page.close()
    compile_recording(recording, root / "bundle", name="worklist")
    return root / "bundle"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "REALISM_AUDIT identity-8: identity is verified per row, never for "
        "uniqueness, so a swapped homonym row is opened and signed"
    ),
)
def test_identical_rows_that_swap_order_never_sign_the_other_chart(
    worklist_bundle, app_url, browser, tmp_path
) -> None:
    """Two rows show the same text; at replay time they have swapped.

    The task opens chart 1001 and signs its note. Signing chart 1002 and
    reporting success acts on the wrong record. The run must halt (the
    target is ambiguous) or sign 1001.
    """

    report, page = _replay(
        browser,
        app_url + "/worklist.html?order=ba",
        worklist_bundle,
        tmp_path / "run",
    )
    try:
        status = page.locator("#signed").inner_text()
        assert not (report.success and status == "Signed chart 1002"), _summary(report)
    finally:
        page.close()
