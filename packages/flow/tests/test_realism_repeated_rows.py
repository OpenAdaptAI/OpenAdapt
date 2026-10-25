"""Worklists without table or list markup, in real Chromium (#1559).

Many apps build a worklist from repeated ``div`` structures: plain div rows,
CSS-grid rows, or cards. Each row's button often has an id that only numbers
its position (``#open-0``). Before the row census learned to see repeated
sibling structures, a recording that opened chart 1001 replayed after the rows
swapped by clicking ``#open-0``, which then opened and signed chart 1002.

Every replay here must halt, or act on the recorded chart. A form whose field
groups repeat must not be mistaken for a worklist.
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
from openadapt_flow.runtime.replayer import Replayer  # noqa: E402
from tests.test_realism_browser import (  # noqa: E402
    _STYLE,
    _center,
    _page,
    _recorder,
    _summary,
)

pytestmark = pytest.mark.timeout(600)

SAME = ("John Smith", "Follow-up", "Dr Lee")
ROWS = {
    "a": ("1001", SAME),
    "b": ("1002", SAME),
    "c": ("1003", ("Maria Garcia", "Intake", "Dr Lee")),
    "d": ("1004", ("Ana Ruiz", "Follow-up", "Dr Kim")),
}

_LAYOUT_STYLE = """
.rows { display: block; }
.row-item { padding: 8px; border: 1px solid #888; margin: 6px; width: 520px; }
.row-item span { margin-right: 18px; }
.grid { display: grid; grid-template-columns: 1fr; gap: 6px; width: 560px; }
.grid-row { display: grid; grid-template-columns: 140px 120px 100px 90px;
            align-items: center; border: 1px solid #888; padding: 6px; }
.cards { display: flex; flex-direction: column; gap: 8px; width: 420px; }
.card { border: 1px solid #555; border-radius: 6px; padding: 10px; }
.card h3 { margin: 0 0 4px 0; font-size: 18px; }
.active { background: #eef; }
"""


def _row_html(layout: str, index: int, chart: str, cells: tuple[str, ...]) -> str:
    # The button id only numbers the row's position; data-chart is the app's
    # own payload, not a record-id attribute the census reads.
    button = (
        f'<button id="open-{index}" data-chart="{chart}" '
        f'onclick="openChart(this)">Open</button>'
    )
    state = " active" if index == 0 else ""
    if layout == "div":
        spans = "".join(f"<span>{cell}</span>" for cell in cells)
        return f'<div class="row-item{state}">{spans}{button}</div>'
    if layout == "grid":
        spans = "".join(f"<div>{cell}</div>" for cell in cells)
        return f'<div class="grid-row{state}">{spans}<div>{button}</div></div>'
    if layout == "bare":
        # No readable identity: a colour swatch the person reads, and "Open".
        colour = {"1001": "#c33", "1002": "#c33", "1003": "#3c3", "1004": "#33c"}
        return (
            f'<div class="row-item{state}"><i style="display:inline-block;'
            f'width:40px;height:16px;background:{colour[chart]}"></i>{button}</div>'
        )
    name, visit, doctor = cells
    return (
        f'<article class="card{state}"><h3>{name}</h3>'
        f"<p>{visit} with {doctor}</p>{button}</article>"
    )


def _worklist(query: dict[str, list[str]]) -> str:
    layout = query.get("layout", ["div"])[0]
    order = query.get("order", ["abc"])[0]
    container = {"div": "rows", "grid": "grid", "card": "cards", "bare": "rows"}[layout]
    rows = "".join(
        _row_html(layout, index, ROWS[key][0], ROWS[key][1])
        for index, key in enumerate(order)
    )
    return f"""<!doctype html><html><head><title>Worklist</title>
<style>{_STYLE}{_LAYOUT_STYLE}</style></head><body>
<h1>Worklist</h1>
<div id="status">No chart open</div>
<div id="chart" style="display:none">
  <button id="sign">Sign visit note</button>
</div>
<div id="signed">Nothing signed</div>
<div class="{container}">{rows}</div>
<script>
let open = null;
function openChart(button) {{
  open = button.dataset.chart;
  document.getElementById('status').textContent = 'Opened chart ' + open;
  document.getElementById('chart').style.display = 'block';
}}
document.getElementById('sign').onclick = () => {{
  document.getElementById('signed').textContent = 'Signed chart ' + open;
}};
</script></body></html>"""


def _menu(query: dict[str, list[str]]) -> str:
    """A menu of uniquely labelled repeated items, then a save."""

    items = "".join(
        f'<div class="nav-item"><a href="#" onclick="pick(this); return false;">'
        f"{label}</a></div>"
        for label in ("Patients", "Reports", "Settings")
    )
    return f"""<!doctype html><html><head><title>Menu</title>
<style>{_STYLE} .nav-item {{ display: inline-block; margin: 8px; }}</style>
</head><body><nav>{items}</nav>
<div id="picked">None</div><button id="save">Save note</button>
<div id="status">Not saved</div>
<script>
function pick(a) {{ document.getElementById('picked').textContent = a.textContent; }}
document.getElementById('save').onclick = () => {{
  document.getElementById('status').textContent =
    'Saved ' + document.getElementById('picked').textContent;
}};
</script></body></html>"""


def _intake(query: dict[str, list[str]]) -> str:
    """A form whose field groups repeat: label, input, hint in each."""

    groups = "".join(
        f'<div class="field"><label for="f{i}">{label}</label>'
        f'<input id="f{i}" autocomplete="off"><small>Required</small></div>'
        for i, label in enumerate(("First name", "Last name", "Phone"))
    )
    return f"""<!doctype html><html><head><title>Intake</title>
<style>{_STYLE} .field {{ margin: 10px 0; }}</style></head><body>
<h1>Intake</h1><form onsubmit="return false">{groups}
<button id="save">Save intake</button></form>
<div id="status">Not saved</div>
<script>
document.getElementById('save').onclick = () => {{
  const v = document.getElementById('f1').value;
  document.getElementById('status').textContent = 'Saved ' + v;
}};
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        page = {
            "/worklist.html": _worklist,
            "/intake.html": _intake,
            "/menu.html": _menu,
        }.get(parsed.path)
        if page is None:
            self.send_error(404)
            return
        body = page(parse_qs(parsed.query)).encode()
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


def _record(app_url, browser, root: Path, layout: str, order: str, chart: str) -> Path:
    url = f"{app_url}/worklist.html?layout={layout}&order={order}"
    page = _page(browser, url)
    try:
        recorder = _recorder(page, root, url)
        recorder.click(*_center(page, f"button[data-chart='{chart}']"))
        assert page.locator("#status").inner_text() == f"Opened chart {chart}"
        recorder.click(*_center(page, "#sign"))
        assert page.locator("#signed").inner_text() == f"Signed chart {chart}"
        recording = recorder.finish()
    finally:
        page.close()
    compile_recording(recording, root / "bundle", name="worklist")
    return root / "bundle"


def _replay(browser, url: str, bundle: Path, run_dir: Path, params=None):
    page = _page(browser, url)
    report = Replayer(PlaywrightBackend(page), poll_interval_s=0.05).run(
        Workflow.load(bundle), params=params or {}, bundle_dir=bundle, run_dir=run_dir
    )
    return report, page


LAYOUTS = ["div", "grid", "card"]


@pytest.mark.parametrize("layout", LAYOUTS)
def test_identical_rows_that_swap_never_sign_the_other_chart(
    app_url, browser, tmp_path, layout
) -> None:
    """Rows 1001 and 1002 read the same; the button ids only number positions."""

    bundle = _record(app_url, browser, tmp_path / "rec", layout, "abc", "1001")
    report, page = _replay(
        browser,
        f"{app_url}/worklist.html?layout={layout}&order=bac",
        bundle,
        tmp_path / "run",
    )
    try:
        signed = page.locator("#signed").inner_text()
        assert signed in {"Nothing signed", "Signed chart 1001"}, _summary(report)
        assert not (report.success and signed != "Signed chart 1001"), _summary(report)
        if signed == "Nothing signed":
            assert not report.success
    finally:
        page.close()


@pytest.mark.parametrize("layout", LAYOUTS)
def test_distinct_rows_that_swap_never_sign_the_other_chart(
    app_url, browser, tmp_path, layout
) -> None:
    """Chart 1003 ("Maria Garcia") recorded; it moves from the third row to the first."""

    bundle = _record(app_url, browser, tmp_path / "rec", layout, "adc", "1003")
    report, page = _replay(
        browser,
        f"{app_url}/worklist.html?layout={layout}&order=cad",
        bundle,
        tmp_path / "run",
    )
    try:
        signed = page.locator("#signed").inner_text()
        assert signed in {"Nothing signed", "Signed chart 1003"}, _summary(report)
        if report.success:
            assert signed == "Signed chart 1003", _summary(report)
    finally:
        page.close()


@pytest.mark.parametrize("layout", LAYOUTS)
def test_distinct_rows_in_the_recorded_order_still_verify(
    app_url, browser, tmp_path, layout
) -> None:
    """No false stop: the recorded row reads differently from its siblings."""

    bundle = _record(app_url, browser, tmp_path / "rec", layout, "adc", "1003")
    report, page = _replay(
        browser,
        f"{app_url}/worklist.html?layout={layout}&order=adc",
        bundle,
        tmp_path / "run",
    )
    try:
        assert report.success, _summary(report)
        assert page.locator("#signed").inner_text() == "Signed chart 1003"
    finally:
        page.close()


def test_repeated_field_groups_are_not_rows(app_url, browser, tmp_path) -> None:
    """A form's repeated label-input groups are not a worklist: it still saves."""

    url = f"{app_url}/intake.html"
    page = _page(browser, url)
    try:
        recorder = _recorder(page, tmp_path, url)
        recorder.click(*_center(page, "#f1"))
        recorder.type_text("Doe", param="last")
        recorder.click(*_center(page, "#save"))
        assert page.locator("#status").inner_text() == "Saved Doe"
        recording = recorder.finish()
    finally:
        page.close()
    compile_recording(recording, tmp_path / "bundle", name="intake")
    report, page = _replay(
        browser, url, tmp_path / "bundle", tmp_path / "run", params={"last": "Roe"}
    )
    try:
        assert report.success, _summary(report)
        assert page.locator("#status").inner_text() == "Saved Roe"
    finally:
        page.close()


def test_rows_with_no_readable_identity_halt_before_the_sign(
    app_url, browser, tmp_path
) -> None:
    """Every row reads only "Open": nothing can tell the rows apart."""

    bundle = _record(app_url, browser, tmp_path / "rec", "bare", "abc", "1001")
    report, page = _replay(
        browser,
        f"{app_url}/worklist.html?layout=bare&order=bac",
        bundle,
        tmp_path / "run",
    )
    try:
        assert not report.success, _summary(report)
        first = report.results[0]
        assert first.identity is not None
        assert first.identity.ambiguity_halt == "identity_uncounted", _summary(report)
        assert page.locator("#status").inner_text() == "No chart open"
        assert page.locator("#signed").inner_text() == "Nothing signed"
    finally:
        page.close()


def test_a_menu_of_unique_items_before_a_save_still_runs(
    app_url, browser, tmp_path
) -> None:
    url = f"{app_url}/menu.html"
    page = _page(browser, url)
    try:
        recorder = _recorder(page, tmp_path, url)
        recorder.click(*_center(page, "text=Reports"))
        recorder.click(*_center(page, "#save"))
        assert page.locator("#status").inner_text() == "Saved Reports"
        recording = recorder.finish()
    finally:
        page.close()
    compile_recording(recording, tmp_path / "bundle", name="menu")
    report, page = _replay(browser, url, tmp_path / "bundle", tmp_path / "run")
    try:
        assert report.success, _summary(report)
        assert page.locator("#status").inner_text() == "Saved Reports"
    finally:
        page.close()
