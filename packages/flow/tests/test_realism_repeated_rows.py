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


# -- review regressions for #1569 ------------------------------------------------
#
# Look-alike rows that differ only in markup (an empty unread dot, an extra
# class, a lone highlighted row, an MRN in a title or a hidden span, a
# differently labelled or hover-revealed action) must still be counted. Cards
# headed by a visit type keep the patient in their identity. Section cards,
# nested cards and icon-only toolbars must not stop.


def _note(*_args: object) -> None:
    """Diagnostics are carried by the assertion messages."""


_P_SAME = "<span>John Smith</span><span>Follow-up</span><span>Dr Lee</span>"
_P_TEXT = {
    "1001": _P_SAME,
    "1002": _P_SAME,
    "1003": "<span>Maria Garcia</span><span>Intake</span><span>Dr Lee</span>",
    "1004": "<span>Ana Ruiz</span><span>Follow-up</span><span>Dr Kim</span>",
}
_P_SCRIPT = """
<div id="status">No chart open</div>
<div id="chart" style="display:none"><button id="sign">Sign visit note</button></div>
<div id="signed">Nothing signed</div>
<script>
let open = null;
function openChart(b) { open = b.dataset.chart;
  document.getElementById('status').textContent = 'Opened chart ' + open;
  document.getElementById('chart').style.display = 'block'; }
document.getElementById('sign').onclick = () => {
  document.getElementById('signed').textContent = 'Signed chart ' + open; };
</script>"""


_P_HOV = {
    "hoveropacity": ".hov button{opacity:0} .hov:hover button{opacity:1}",
    "hovervis": ".hov button{visibility:hidden} .hov:hover button{visibility:visible}",
}


def _p_btn(i, chart, label="Open"):
    return f'<button id="open-{i}" data-chart="{chart}" onclick="openChart(this)">{label}</button>'


def _probe_page(q):
    s = q["s"][0]
    order = q.get("order", ["abcd"])[0]
    charts = [{"a": "1001", "b": "1002", "c": "1003", "d": "1004"}[k] for k in order]
    rows = []
    for i, c in enumerate(charts):
        if s == "iconshape":
            # 1001 and 1004 carry an empty unread dot; 1002/1003 do not.
            dot = '<i class="dot"></i>' if c in ("1001", "1004") else ""
            rows.append(f'<div class="row-item">{dot}{_P_TEXT[c]}{_p_btn(i, c)}</div>')
        elif s == "stateclass":
            extra = " unread" if c in ("1001", "1004") else ""
            rows.append(
                f'<div class="row-item{extra}">{_P_TEXT[c]}{_p_btn(i, c)}</div>'
            )
        elif s == "loneclass":
            # Only the last-opened row carries a highlight class.
            extra = " table-active" if c == "1001" else ""
            rows.append(
                f'<div class="row-item{extra}">{_P_TEXT[c]}{_p_btn(i, c)}</div>'
            )
        elif s == "attr":
            # Identical text; the MRN only in a title attribute.
            rows.append(
                f'<div class="row-item"><span title="MRN {c}">&#9679;</span>'
                f"{_P_TEXT[c]}{_p_btn(i, c)}</div>"
            )
        elif s in ("labeldiff", "labelcase"):
            lab = "Open"
            if c == "1002":
                lab = "Resume" if s == "labeldiff" else "OPEN"
            rows.append(f'<div class="row-item">{_P_TEXT[c]}{_p_btn(i, c, lab)}</div>')
        elif s in ("hoveropacity", "hovervis"):
            rows.append(f'<div class="row-item hov">{_P_TEXT[c]}{_p_btn(i, c)}</div>')
        elif s == "headingvisit":
            visit, name = {
                "1001": ("Follow-up", "John Smith"),
                "1002": ("Physical", "John Smith"),
                "1003": ("Intake", "Maria Garcia"),
                "1004": ("Follow-up", "Ana Ruiz"),
            }[c]
            rows.append(
                f'<article class="card"><h3>{visit}</h3><p>{name} with Dr Lee</p>{_p_btn(i, c)}</article>'
            )
        elif s == "hidden":
            rows.append(
                f'<div class="row-item">{_P_TEXT[c]}<span style="display:none">{c}</span>'
                f"{_p_btn(i, c)}</div>"
            )
    body = "".join(rows)
    if s == "nested":
        # Patient cards; each holds repeated document lines with "Open".
        names = {
            "1001": "John Smith",
            "1002": "Ana Ruiz",
            "1003": "Maria Garcia",
            "1004": "Li Wei",
        }
        cards = []
        for i, c in enumerate(charts[:2]):
            cards.append(
                f'<section class="card"><h3>{names[c]}</h3>'
                f'<div class="line"><span>Visit note</span>{_p_btn(i, c)}</div>'
                f'<div class="line"><span>Labs</span>'
                f'<button onclick="0">Open</button></div></section>'
            )
        body = "".join(cards)
    if s == "perf":
        n = int(q["n"][0])
        body = "".join(
            f'<div class="row-item"><span>Patient {k}</span><span>MRN {100000 + k}</span>'
            f"<span>Dr Lee</span>{_p_btn(k, str(k))}</div>"
            for k in range(n)
        )
    if s == "icontoolbar":
        body = (
            '<div class="toolbar">'
            + "".join(
                f'<span class="tool"><button id="t{k}" title="{k}" '
                f"onclick=\"document.getElementById('fmt').textContent='{k}'\">"
                f'<svg width="16" height="16"><rect width="16" height="16"/></svg>'
                f"</button></span>"
                for k in ("bold", "italic", "under")
            )
            + '</div><div id="fmt">none</div>'
            '<button id="save" onclick="document.getElementById(\'status\').textContent='
            "'Saved '+document.getElementById('fmt').textContent\">Save</button>"
            '<div id="status">Not saved</div>'
        )
        return f"<!doctype html><html><head><style>{_STYLE}</style></head><body>{body}</body></html>"
    if s == "sections":
        # OpenEMR-like dashboard: section cards, each with an Edit button and
        # patient-specific content.  pid picks the patient.
        pid = q.get("pid", ["A"])[0]
        data = {
            "A": ("Penicillin", "Metformin 500mg", "Asthma"),
            "B": ("None known", "Lisinopril 10mg", "Hypertension"),
        }[pid]
        secs = "".join(
            f'<div class="section"><div class="hdr"><span>{t}</span>'
            f'<a href="#" id="edit-{k}" onclick="ed(\'{t}\');return false">Edit</a></div>'
            f'<div class="body">{d}</div></div>'
            for k, (t, d) in enumerate(
                zip(("Allergies", "Medications", "Problems"), data)
            )
        )
        if q.get("empty"):
            secs += (
                '<div class="section"><div class="hdr"><span></span>'
                '<a href="#" id="edit-9" onclick="return false">Edit</a></div>'
                f'<div class="body">{pid} notes</div></div>'
            )
        return f"""<!doctype html><html><head><style>{_STYLE}</style></head><body>
<h1>Patient {pid}</h1>{secs}
<div id="editor" style="display:none"><input id="val"><button id="save">Save</button></div>
<div id="status">Not saved</div>
<script>let sec=null;function ed(t){{sec=t;document.getElementById('editor').style.display='block';}}
document.getElementById('save').onclick=()=>{{document.getElementById('status').textContent=
'Saved '+sec+' '+document.getElementById('val').value;}};</script></body></html>"""
    return (
        f"<!doctype html><html><head><style>{_STYLE}"
        ".row-item{padding:8px;border:1px solid #888;margin:6px;width:560px}"
        ".row-item span{margin-right:14px} .dot{display:inline-block;width:8px;"
        "height:8px;background:blue;border-radius:4px;margin-right:6px}"
        ".card{border:1px solid #555;margin:8px;padding:8px;width:400px}"
        f"{_P_HOV.get(s, '')}"
        f"</style></head><body><h1>Worklist</h1>{_P_SCRIPT}"
        f'<div class="rows">{body}</div></body></html>'
    )


class _ProbeHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        b = _probe_page(parse_qs(urlparse(self.path).query)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def probe_app():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _ProbeHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/p"
    srv.shutdown()


def _summ(report):
    out = []
    for r in report.results:
        idn = r.identity
        out.append(
            f"{r.step_id} ok={r.ok} halt={getattr(idn, 'ambiguity_halt', None)} "
            f"status={getattr(idn, 'status', None)} err={(r.error or '')[:160]}"
        )
    return "\n".join(out)


def _rec_open_sign(probe_app, browser, root, s, order, chart):
    url = f"{probe_app}?s={s}&order={order}"
    page = _page(browser, url)
    try:
        rec = _recorder(page, root, url)
        rec.click(*_center(page, f"button[data-chart='{chart}']"))
        rec.click(*_center(page, "#sign"))
        assert page.locator("#signed").inner_text() == f"Signed chart {chart}"
        recording = rec.finish()
    finally:
        page.close()
    compile_recording(recording, root / "bundle", name="w")
    return root / "bundle"


@pytest.mark.parametrize(
    "s", ["iconshape", "stateclass", "loneclass", "attr", "hidden"]
)
def test_look_alike_rows_that_differ_in_markup_never_sign_the_other_chart(
    probe_app, browser, tmp_path, s
):
    bundle = _rec_open_sign(probe_app, browser, tmp_path / "rec", s, "abcd", "1001")
    report, page = _replay(
        browser, f"{probe_app}?s={s}&order=bacd", bundle, tmp_path / "run"
    )
    signed = page.locator("#signed").inner_text()
    _note(f"\n[{s}] success={report.success} signed={signed!r}\n{_summ(report)}")
    page.close()
    assert signed in {"Nothing signed", "Signed chart 1001"}, f"WRONG RECORD: {signed}"


@pytest.mark.parametrize("s", ["iconshape", "stateclass", "attr", "hidden"])
def test_look_alike_markup_rows_in_the_recorded_order_still_verify(
    probe_app, browser, tmp_path, s
):
    bundle = _rec_open_sign(probe_app, browser, tmp_path / "rec", s, "cbad", "1003")
    report, page = _replay(
        browser, f"{probe_app}?s={s}&order=cbad", bundle, tmp_path / "run"
    )
    signed = page.locator("#signed").inner_text()
    _note(f"\n[{s} same] success={report.success} signed={signed!r}\n{_summ(report)}")
    page.close()
    assert report.success and signed == "Signed chart 1003"


def test_nested_patient_cards_in_the_recorded_order_still_verify(
    probe_app, browser, tmp_path
):
    bundle = _rec_open_sign(
        probe_app, browser, tmp_path / "rec", "nested", "abcd", "1001"
    )
    report, page = _replay(
        browser, f"{probe_app}?s=nested&order=abcd", bundle, tmp_path / "run"
    )
    signed = page.locator("#signed").inner_text()
    _note(
        f"\n[nested same] success={report.success} signed={signed!r}\n{_summ(report)}"
    )
    page.close()
    assert report.success and signed == "Signed chart 1001"


def test_nested_patient_cards_that_swap_never_sign_the_other_chart(
    probe_app, browser, tmp_path
):
    bundle = _rec_open_sign(
        probe_app, browser, tmp_path / "rec", "nested", "abcd", "1001"
    )
    report, page = _replay(
        browser, f"{probe_app}?s=nested&order=bacd", bundle, tmp_path / "run"
    )
    signed = page.locator("#signed").inner_text()
    _note(
        f"\n[nested swap] success={report.success} signed={signed!r}\n{_summ(report)}"
    )
    page.close()
    assert signed in {"Nothing signed", "Signed chart 1001"}


def test_an_icon_only_toolbar_before_a_save_still_runs(probe_app, browser, tmp_path):
    url = f"{probe_app}?s=icontoolbar"
    page = _page(browser, url)
    rec = _recorder(page, tmp_path, url)
    rec.click(*_center(page, "#titalic"))
    rec.click(*_center(page, "#save"))
    assert page.locator("#status").inner_text() == "Saved italic"
    recording = rec.finish()
    page.close()
    compile_recording(recording, tmp_path / "bundle", name="t")
    report, page = _replay(browser, url, tmp_path / "bundle", tmp_path / "run")
    st = page.locator("#status").inner_text()
    _note(f"\n[icontoolbar] success={report.success} status={st!r}\n{_summ(report)}")
    page.close()
    assert report.success and st == "Saved italic"


@pytest.mark.parametrize("replay_pid", ["A", "B"])
def test_section_cards_edit_on_another_patient_still_runs(
    probe_app, browser, tmp_path, replay_pid
):
    url = f"{probe_app}?s=sections&pid=A"
    page = _page(browser, url)
    rec = _recorder(page, tmp_path, url)
    rec.click(*_center(page, "#edit-1"))
    rec.click(*_center(page, "#val"))
    rec.type_text("Aspirin", param="med")
    rec.click(*_center(page, "#save"))
    assert page.locator("#status").inner_text() == "Saved Medications Aspirin"
    recording = rec.finish()
    page.close()
    compile_recording(recording, tmp_path / "bundle", name="sec")
    report, page = _replay(
        browser,
        f"{probe_app}?s=sections&pid={replay_pid}",
        tmp_path / "bundle",
        tmp_path / "run",
        params={"med": "Ibuprofen"},
    )
    st = page.locator("#status").inner_text()
    _note(
        f"\n[sections {replay_pid}] success={report.success} status={st!r}\n{_summ(report)}"
    )
    page.close()
    assert report.success and st == "Saved Medications Ibuprofen"


@pytest.mark.parametrize("s", ["labeldiff", "labelcase", "hoveropacity", "hovervis"])
def test_rows_whose_actions_differ_or_hide_never_sign_the_other_chart(
    probe_app, browser, tmp_path, s
):
    test_look_alike_rows_that_differ_in_markup_never_sign_the_other_chart(
        probe_app, browser, tmp_path, s
    )


def test_cards_headed_by_visit_type_never_sign_the_other_chart(
    probe_app, browser, tmp_path
):
    bundle = _rec_open_sign(
        probe_app, browser, tmp_path / "rec", "headingvisit", "acb", "1001"
    )
    report, page = _replay(
        browser, f"{probe_app}?s=headingvisit&order=dcb", bundle, tmp_path / "run"
    )
    signed = page.locator("#signed").inner_text()
    _note(
        f"\n[headingvisit] success={report.success} signed={signed!r}\n{_summ(report)}"
    )
    page.close()
    assert signed in {"Nothing signed", "Signed chart 1001"}, f"WRONG RECORD: {signed}"


def test_section_cards_with_an_empty_heading_still_run(probe_app, browser, tmp_path):
    url = f"{probe_app}?s=sections&pid=A&empty=1"
    page = _page(browser, url)
    rec = _recorder(page, tmp_path, url)
    rec.click(*_center(page, "#edit-1"))
    rec.click(*_center(page, "#val"))
    rec.type_text("Aspirin", param="med")
    rec.click(*_center(page, "#save"))
    recording = rec.finish()
    page.close()
    compile_recording(recording, tmp_path / "bundle", name="sec")
    report, page = _replay(
        browser,
        f"{probe_app}?s=sections&pid=B&empty=1",
        tmp_path / "bundle",
        tmp_path / "run",
        params={"med": "Ibuprofen"},
    )
    st = page.locator("#status").inner_text()
    _note(
        f"\n[sections-empty B] success={report.success} status={st!r}\n{_summ(report)}"
    )
    page.close()
    assert report.success and st == "Saved Medications Ibuprofen"


def test_a_1500_row_census_finishes_in_under_a_second(probe_app, browser) -> None:
    """The census reads each sibling once: a long worklist stays fast."""

    import time

    page = _page(browser, f"{probe_app}?s=perf&n=1500")
    try:
        backend = PlaywrightBackend(page)
        page.locator("#open-3").scroll_into_view_if_needed()
        x, y = _center(page, "#open-3")
        started = time.perf_counter()
        census = backend.identity_candidates_at(x, y)
        elapsed = time.perf_counter() - started
    finally:
        page.close()
    assert census is not None and len(census["candidates"]) == 1
    assert elapsed < 1.0, f"census took {elapsed:.2f}s"
