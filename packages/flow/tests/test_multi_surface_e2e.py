"""Multi-surface workflows, end to end (docs/MULTI_SURFACE.md).

A local fixture app opens a popup (``window.open``) and a second tab (a
``target=_blank`` link). The UNMODIFIED Recorder -> compiler -> Replayer stack
records the demonstration across all three pages, compiles one bundle, and
replays it on a fresh browser with zero model calls. Refusal cases: an
unexpected popup, a declared popup that never opens, and a popup on a
different URL all halt with a reason and drive nothing else.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("playwright.sync_api")

from openadapt_flow.backends.playwright_backend import PlaywrightBackend  # noqa: E402
from openadapt_flow.compiler import compile_recording  # noqa: E402
from openadapt_flow.ir import ActionKind, Workflow  # noqa: E402
from openadapt_flow.recorder import Recorder  # noqa: E402
from openadapt_flow.runtime.replayer import Replayer  # noqa: E402

pytestmark = pytest.mark.timeout(600)

VIEWPORT = {"width": 1280, "height": 800}

_STYLE = (
    "<style>body{font-family:Arial,sans-serif;margin:40px}"
    "h1{font-size:34px}button,a.btn{display:inline-block;font-size:24px;"
    "padding:14px 28px;margin:16px 16px 16px 0;border:2px solid #333;"
    "background:#eee;color:#000;text-decoration:none}"
    "p{font-size:24px}</style>"
)


def _index(query: dict[str, list[str]]) -> str:
    popup_path = query.get("popup", ["/popup.html"])[0]
    no_popup = "nopopup" in query
    surprise = "surprise" in query
    open_popup = (
        "document.getElementById('popup-status').textContent='Nothing opened';"
        if no_popup
        else f"window.open('{popup_path}','approval');"
    )
    surprise_js = "window.open('/promo.html','promo');" if surprise else ""
    return f"""<!doctype html><html><head><title>Referral Portal</title>
{_STYLE}</head><body>
<h1>Referral Portal Home</h1>
<button id="open-popup" onclick="{open_popup}">Request Approval</button>
<a id="open-report" class="btn" href="/report.html" target="_blank">Open Report</a>
<p id="popup-status">Approval pending</p>
<button id="finish" onclick="{surprise_js}document.getElementById('done').textContent='Referral Finished';">Finish Referral</button>
<p id="done"></p>
</body></html>"""


_POPUP = f"""<!doctype html><html><head><title>Approval Window</title>
{_STYLE}</head><body>
<h1>Approval Window Ready</h1>
<button id="approve" onclick="window.opener.document.getElementById('popup-status').textContent='Approval Granted';document.getElementById('sent').textContent='Approval Sent Back';">Approve Request</button>
<p id="sent"></p>
</body></html>"""

_OTHER = f"""<!doctype html><html><head><title>Approval Window</title>
{_STYLE}</head><body><h1>Approval Window Ready</h1>
<button id="approve">Approve Request</button></body></html>"""

_REPORT = f"""<!doctype html><html><head><title>Quarterly Report</title>
{_STYLE}</head><body>
<h1>Quarterly Report Viewer</h1>
<button id="reviewed" onclick="document.getElementById('ack').textContent='Review Recorded Today';">Mark Reviewed</button>
<p id="ack"></p>
</body></html>"""

_SELFCLOSE = f"""<!doctype html><html><head><title>Sign In Window</title>
{_STYLE}</head><body>
<h1>Sign In Window Ready</h1>
<p id="terms">Terms Pending</p>
<button id="agree" onclick="document.getElementById('terms').textContent='Terms Accepted';">Accept Terms</button>
<button id="approve-close" onclick="window.opener.document.getElementById('popup-status').textContent='Signed In Elsewhere';window.close();">Approve And Close</button>
</body></html>"""

_CONFIRM = f"""<!doctype html><html><head><title>Record Desk</title>
{_STYLE}</head><body>
<h1>Record Desk Ready</h1>
<p id="record-status">Record Present</p>
<button id="delete" onclick="const m=new URLSearchParams(location.search).get('msg')||'Delete record 42?';document.getElementById('record-status').textContent=confirm(m)?'Record Deleted':'Record Kept';">Delete Record</button>
</body></html>"""

_PROMO = f"""<!doctype html><html><head><title>Special Offer</title>
{_STYLE}</head><body><h1>Unexpected Offer Window</h1></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        parts = urlsplit(self.path)
        pages = {
            "/popup.html": _POPUP,
            "/other.html": _OTHER,
            "/report.html": _REPORT,
            "/promo.html": _PROMO,
            "/selfclose.html": _SELFCLOSE,
            "/confirm.html": _CONFIRM,
        }
        if parts.path in ("/", "/index.html"):
            body = _index(parse_qs(parts.query))
        elif parts.path in pages:
            body = pages[parts.path]
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture(scope="module")
def app_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="module")
def browser() -> Iterator[object]:
    from openadapt_flow._browser_setup import ensure_chromium_installed

    ensure_chromium_installed()
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        chromium = pw.chromium.launch(headless=True)
        yield chromium
        chromium.close()


def _fresh_page(browser: object, url: str):
    page = browser.new_page(viewport=VIEWPORT, device_scale_factor=1)  # type: ignore[attr-defined]
    page.goto(url)
    page.wait_for_load_state("load")
    return page


def _center(page, selector: str) -> tuple[int, int]:
    box = page.locator(selector).bounding_box()
    assert box is not None, selector
    return int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)


def _record(recorder: Recorder, primary_page) -> None:
    recorder.click(*_center(primary_page, "#open-popup"))
    assert recorder.surface_ids == ("main", "popup_1")
    recorder.switch_surface("popup_1")
    popup_page = recorder.surface_backend("popup_1").page  # type: ignore[attr-defined]
    recorder.click(*_center(popup_page, "#approve"))
    recorder.switch_surface("main")
    recorder.click(*_center(primary_page, "#open-report"))
    assert recorder.surface_ids == ("main", "popup_1", "tab_1")
    recorder.switch_surface("tab_1")
    report_page = recorder.surface_backend("tab_1").page  # type: ignore[attr-defined]
    recorder.click(*_center(report_page, "#reviewed"))
    recorder.switch_surface("main")
    recorder.click(*_center(primary_page, "#finish"))


@pytest.fixture(scope="module")
def bundle(app_url: str, browser: object, tmp_path_factory) -> tuple[Path, Workflow]:
    root = tmp_path_factory.mktemp("multi_surface")
    page = _fresh_page(browser, app_url)
    try:
        recorder = Recorder(
            PlaywrightBackend(page),
            root / "recording",
            app_url=app_url,
            settle_interval_s=0.05,
            settle_timeout_s=2.0,
        )
        _record(recorder, page)
        recording = recorder.finish()
    finally:
        page.context.close()
    workflow = compile_recording(recording, root / "bundle", name="multi-surface")
    return root / "bundle", workflow


def _replay(
    browser: object, url: str, bundle_dir: Path, run_dir: Path
) -> tuple[object, object]:
    page = _fresh_page(browser, url)
    report = Replayer(PlaywrightBackend(page), poll_interval_s=0.05).run(
        Workflow.load(bundle_dir), bundle_dir=bundle_dir, run_dir=run_dir
    )
    return report, page


def _summary(report) -> str:
    return "\n".join(f"{r.step_id} ok={r.ok} err={r.error}" for r in report.results)


def test_compiles_one_bundle_across_popup_and_tab(bundle) -> None:
    _bundle_dir, workflow = bundle
    assert [s.id for s in workflow.surfaces] == ["main", "popup_1", "tab_1"]
    popup = workflow.surfaces[1]
    tab = workflow.surfaces[2]
    assert popup.opened_by_step == "step_000" and popup.opener == "main"
    assert popup.opener_link == "window_opener"
    assert tab.opener_link == "none"  # target=_blank implies noopener
    assert popup.url_pattern is not None and popup.url_pattern.endswith(
        r"popup\.html(?:[?#].*)?"
    )
    # Titles are not recorded (they carry record names), so not asserted.
    assert popup.title_pattern is None
    actions = [(s.action, s.surface) for s in workflow.steps]
    assert actions == [
        (ActionKind.CLICK, "main"),
        (ActionKind.SWITCH_SURFACE, "popup_1"),
        (ActionKind.CLICK, "popup_1"),
        (ActionKind.SWITCH_SURFACE, "main"),
        (ActionKind.CLICK, "main"),
        (ActionKind.SWITCH_SURFACE, "tab_1"),
        (ActionKind.CLICK, "tab_1"),
        (ActionKind.SWITCH_SURFACE, "main"),
        (ActionKind.CLICK, "main"),
    ]
    assert workflow.steps[0].opens == ["popup_1"]
    assert workflow.steps[4].opens == ["tab_1"]
    for step in workflow.steps:
        if step.action is ActionKind.SWITCH_SURFACE:
            assert step.risk == "reversible"
            assert step.surface_origin is not None
            assert step.identity_armed is True
    # The bundle round-trips through its sealed manifest.
    loaded = Workflow.load(_bundle_dir)
    assert loaded.surfaces == workflow.surfaces


def test_replays_across_popup_and_tab_with_zero_model_calls(
    bundle, app_url: str, browser: object, tmp_path
) -> None:
    bundle_dir, _ = bundle
    report, page = _replay(browser, app_url, bundle_dir, tmp_path / "run")
    try:
        assert report.success, _summary(report)
        assert report.model_calls == 0
        # The demo profile never claims VERIFIED (that needs a governed run
        # with independently confirmed effects; see
        # test_multi_surface_governed.py); every step's own contract held.
        assert report.execution_outcome == "COMPLETED_UNVERIFIED"
        assert all(r.postconditions_ok is not False for r in report.results)
        # The popup's click wrote back into the opener, the report tab got its
        # click, and the final click ran on the main page again.
        assert page.locator("#popup-status").inner_text() == "Approval Granted"
        assert page.locator("#done").inner_text() == "Referral Finished"
        pages = page.context.pages
        assert len(pages) == 3
        report_page = pages[2]
        assert report_page.locator("#ack").inner_text() == "Review Recorded Today"
        switch_results = [
            r for r in report.results if r.step_id in ("step_001", "step_003")
        ]
        assert all(r.ok and r.postconditions_ok for r in switch_results)
        # Every result records the surface it ran on.
        assert [r.surface for r in report.results][:2] == ["main", "popup_1"]
    finally:
        page.context.close()


def test_unexpected_popup_halts_without_guessing(
    bundle, app_url: str, browser: object, tmp_path
) -> None:
    bundle_dir, _ = bundle
    report, page = _replay(
        browser, f"{app_url}/?surprise=1", bundle_dir, tmp_path / "run"
    )
    try:
        assert not report.success
        last = report.results[-1]
        assert last.step_id == "step_008"
        assert last.safety_halt and last.failure_category == "safety_halt"
        assert "no step of this workflow declares" in (last.error or "")
        # The message never quotes the page's URL or title.
        assert "promo" not in (last.error or "").lower()
        assert "Offer" not in (last.error or "")
    finally:
        page.context.close()


def test_declared_popup_that_never_opens_halts(
    bundle, app_url: str, browser: object, tmp_path
) -> None:
    bundle_dir, _ = bundle
    workflow = Workflow.load(bundle_dir)
    workflow.steps[0].timeout_s = 1.0
    # The compiled new_tab_opened postcondition already catches this; drop it
    # so the test exercises the surface check that backs it up.
    workflow.steps[0].expect = []
    page = _fresh_page(browser, f"{app_url}/?nopopup=1")
    try:
        report = Replayer(PlaywrightBackend(page), poll_interval_s=0.05).run(
            workflow, bundle_dir=bundle_dir, run_dir=tmp_path / "run"
        )
        assert not report.success
        assert len(report.results) == 1
        assert "expected to open 1 browser page(s)" in (report.results[0].error or "")
        # Nothing ran on the opener after the refusal.
        assert page.locator("#done").inner_text() == ""
    finally:
        page.context.close()


def test_popup_on_a_different_url_halts(
    bundle, app_url: str, browser: object, tmp_path
) -> None:
    bundle_dir, _ = bundle
    report, page = _replay(
        browser, f"{app_url}/?popup=/other.html", bundle_dir, tmp_path / "run"
    )
    try:
        assert not report.success
        assert report.results[-1].step_id == "step_000"
        assert "does not match the recorded pattern" in (report.results[-1].error or "")
        assert "other" not in (report.results[-1].error or "")
    finally:
        page.context.close()


def test_recorded_surfaces_keep_no_query_string_or_title(
    app_url: str, browser: object, tmp_path
) -> None:
    import json

    # An OAuth-style popup: the code and state ride in the query string.
    popup = "/popup.html?code=oauth-secret-code&state=state-token"
    page = _fresh_page(browser, f"{app_url}/?popup={popup.replace('&', '%26')}")
    try:
        recorder = Recorder(
            PlaywrightBackend(page),
            tmp_path / "recording",
            app_url=app_url,
            settle_interval_s=0.05,
            settle_timeout_s=2.0,
        )
        recorder.click(*_center(page, "#open-popup"))
        assert recorder.surface_ids == ("main", "popup_1")
        recording = recorder.finish()
    finally:
        page.context.close()
    meta_text = (recording / "meta.json").read_text()
    [entry] = [s for s in json.loads(meta_text)["surfaces"] if s["id"] == "popup_1"]
    assert entry["url"] == f"{app_url}/popup.html"
    assert "title" not in entry
    assert "oauth-secret-code" not in meta_text and "state-token" not in meta_text
    assert "Approval Window" not in meta_text
