"""Browser page-signal settle and DOM text checks.

The page-quiescence settle and the DOM text_present check replace image work
on the browser backend only when the page can PROVE the stronger condition;
everything else falls back to the screenshot settle / OCR. These tests pin:

* a half-loaded page (request in flight, DOM still being built, animation
  running) never reports settled from page signals, and content that arrives
  late is present in the frame the settle returns;
* signals that cannot be judged (visible canvas) hand over to screenshots;
* DOM text presence needs visible, uncovered, in-viewport text, and a DOM miss
  never passes a postcondition by itself.
"""

from __future__ import annotations

import time

import pytest

from openadapt_flow.vision import settle as settle_mod


@pytest.fixture(scope="module")
def browser():
    sync = pytest.importorskip("playwright.sync_api")
    with sync.sync_playwright() as pw:
        try:
            b = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - no browser installed
            pytest.skip(f"chromium unavailable: {exc}")
        yield b
        b.close()


class _Server:
    """Loopback HTTP server: ``/`` serves the page, ``/slow`` answers late."""

    def __init__(self, html: str, delay_s: float) -> None:
        import http.server
        import threading

        body = html.encode()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path == "/slow":
                    time.sleep(delay_s)
                    payload, ctype = b"LATE CONTENT", "text/plain"
                else:
                    payload, ctype = body, "text/html"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()


class _Ctx:
    def __init__(self, context, server) -> None:
        self.context, self.server = context, server

    def close(self) -> None:
        self.context.close()
        self.server.close()


def _backend(browser, html: str, *, route_delay_s: float = 0.0):
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    server = _Server(html, route_delay_s)
    context = browser.new_context(
        viewport={"width": 640, "height": 400}, device_scale_factor=1
    )
    page = context.new_page()
    backend = PlaywrightBackend(page)  # arms tracking before navigation
    page.goto(server.url, wait_until="domcontentloaded")
    return backend, page, _Ctx(context, server)


_HALF_LOADED = """<!doctype html><html><body>
<p id=s>loading...</p>
<script>
fetch('/slow').then(r => r.text()).then(t => {
  const b = document.createElement('button'); b.textContent = t;
  document.body.appendChild(b); document.getElementById('s').remove();
});
</script></body></html>"""


def test_inflight_request_blocks_page_settle(browser) -> None:
    backend, page, ctx = _backend(browser, _HALF_LOADED, route_delay_s=0.8)
    try:
        assert backend.page_quiescent(0.1) is False  # request still in flight
        result = settle_mod.wait_settled_result(backend, timeout_s=5.0)
        assert result.settled
        # The settle returned only after the late content was built.
        assert page.locator("button", has_text="LATE CONTENT").count() == 1
        assert page.locator("#s").count() == 0
    finally:
        ctx.close()


_CHURN = """<!doctype html><html><body><p id=c>0</p>
<script>let n = 0; setInterval(() => {
  document.getElementById('c').textContent = String(++n); }, 30);</script>
</body></html>"""


def test_continuous_dom_churn_never_page_settles(browser) -> None:
    backend, _page, ctx = _backend(browser, _CHURN)
    try:
        time.sleep(0.3)
        for _ in range(5):
            assert backend.page_quiescent(0.1) is False
            time.sleep(0.03)
        # Never settled, although a one-digit change can be below the
        # screenshot rule's perceptual-hash resolution (two "identical"
        # frames): page signals are authoritative while available.
        result = settle_mod.wait_settled_result(backend, timeout_s=0.8)
        assert result.settled is False
        assert result.mode == "page"
    finally:
        ctx.close()


_ANIMATING = """<!doctype html><html><head><style>
@keyframes spin { from { transform: rotate(0) } to { transform: rotate(360deg) } }
#x { width: 40px; height: 40px; background: red; animation: spin 1s infinite linear }
</style></head><body><div id=x></div></body></html>"""


def test_running_animation_blocks_page_settle(browser) -> None:
    backend, _page, ctx = _backend(browser, _ANIMATING)
    try:
        time.sleep(0.3)
        assert backend.page_quiescent(0.1) is False
        assert backend._quiescence.last_reason == "busy:animation"
    finally:
        ctx.close()


def test_visible_canvas_hands_over_to_screenshots(browser) -> None:
    html = "<!doctype html><canvas width=50 height=50></canvas>"
    backend, _page, ctx = _backend(browser, html)
    try:
        time.sleep(0.3)
        assert backend.page_quiescent(0.1) is None
        result = settle_mod.wait_settled_result(backend, timeout_s=2.0)
        assert result.settled and result.mode == "pixel"
    finally:
        ctx.close()


def test_static_page_settles_from_page_signals(browser) -> None:
    backend, _page, ctx = _backend(browser, "<!doctype html><p>static</p>")
    try:
        time.sleep(0.3)
        result = settle_mod.wait_settled_result(backend, timeout_s=2.0)
        assert result.settled and result.mode == "page"
        assert result.png.startswith(b"\x89PNG")
    finally:
        ctx.close()


def test_input_event_restarts_the_quiet_window(browser) -> None:
    html = "<!doctype html><input id=i>"
    backend, page, ctx = _backend(browser, html)
    try:
        time.sleep(0.3)
        assert backend.page_quiescent(0.1) is True
        page.focus("#i")
        page.keyboard.type("x")
        assert backend.page_quiescent(0.1) is False
    finally:
        ctx.close()


_TEXT = """<!doctype html><html><body style="margin:0">
<p id=vis>Visible words here</p>
<p style="display:none">Hidden words</p>
<p style="opacity:0">Transparent words</p>
<p style="position:absolute; top:2000px">Far below words</p>
<p id=under style="position:absolute; top:100px">Covered words</p>
<div style="position:absolute; top:90px; left:0; width:600px; height:60px;
  background:white"></div>
<iframe style="position:absolute; top:200px; width:300px; height:80px"
  srcdoc="<p>Framed words</p>"></iframe>
<iframe style="display:none" srcdoc="<p>Hidden frame words</p>"></iframe>
</body></html>"""


def test_dom_text_presence_requires_visible_uncovered_text(browser) -> None:
    backend, _page, ctx = _backend(browser, _TEXT)
    try:
        time.sleep(0.3)
        assert backend.visible_text_present("Visible words here") is True
        assert backend.visible_text_present("visiblewords") is True  # squash
        # OCR-recorded texts misread punctuation; letters and digits decide.
        assert backend.visible_text_present("Visible-words, here.") is True
        assert backend.visible_text_present("Visible words theirs") is None
        assert backend.visible_text_present("Framed words") is True
        for absent in (
            "Hidden words",
            "Transparent words",
            "Far below words",
            "Covered words",
            "Hidden frame words",
            "Not on the page",
        ):
            # Never False: a DOM miss is "not proven", not "absent".
            assert backend.visible_text_present(absent) is None, absent
    finally:
        ctx.close()


def test_late_text_is_not_present_before_it_arrives(browser) -> None:
    backend, _page, ctx = _backend(browser, _HALF_LOADED, route_delay_s=0.8)
    try:
        assert backend.visible_text_present("LATE CONTENT") is None
        settle_mod.wait_settled_result(backend, timeout_s=5.0)
        assert backend.visible_text_present("LATE CONTENT") is True
    finally:
        ctx.close()


# --- replayer wiring: DOM proves presence, DOM miss falls to OCR ----------


class _Vision:
    def __init__(self, ocr_answer: bool) -> None:
        self.ocr_answer = ocr_answer
        self.calls = 0

    def text_present(self, png, text, **_kw):
        self.calls += 1
        return self.ocr_answer


class _DomBackend:
    def __init__(self, answer) -> None:
        self.answer = answer

    def visible_text_present(self, text):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _passes(backend, vision, kind: str) -> bool:
    from openadapt_flow.ir import Postcondition, PostconditionKind
    from openadapt_flow.runtime.replayer import Replayer

    replayer = Replayer.__new__(Replayer)
    replayer.backend = backend
    replayer.vision = vision
    pc = Postcondition(kind=PostconditionKind(kind), text="Saved")
    return replayer._postcondition_passes(pc, b"png", None)


@pytest.mark.parametrize("dom", [None, False, RuntimeError("boom")])
def test_dom_miss_never_passes_text_present_on_its_own(dom) -> None:
    vision = _Vision(ocr_answer=False)
    assert _passes(_DomBackend(dom), vision, "text_present") is False
    assert vision.calls == 1  # fell back to OCR


def test_dom_hit_passes_without_ocr() -> None:
    vision = _Vision(ocr_answer=False)
    assert _passes(_DomBackend(True), vision, "text_present") is True
    assert vision.calls == 0


def test_text_absent_is_still_judged_by_ocr() -> None:
    vision = _Vision(ocr_answer=True)
    assert _passes(_DomBackend(None), vision, "text_absent") is False
    assert vision.calls == 1


def test_half_loaded_page_blocks_action_under_readiness_gate(browser) -> None:
    """A page waiting on a slow request is "not settled" when the budget ends,
    which the replayer's readiness gate turns into a halt before acting --
    even though its pixels (a static placeholder) are perfectly stable."""
    from openadapt_flow.runtime.replayer import Replayer

    backend, page, ctx = _backend(browser, _HALF_LOADED, route_delay_s=2.0)
    try:
        time.sleep(0.3)  # placeholder rendered, request in flight
        replayer = Replayer.__new__(Replayer)
        replayer.backend = backend
        replayer.vision = settle_mod
        replayer.require_settled = True
        replayer.poll_interval_s = 0.1
        replayer.settle_readiness_timeout_s = 0.5
        gate = replayer._wait_starting_state_settled(b"")
        assert gate[1] is not None and "never stopped changing" in gate[1]
        assert page.locator("button").count() == 0
    finally:
        ctx.close()


_FOCUS_RING = """<!doctype html><html><head><style>
input { transition: box-shadow 1.5s, border-color 1.5s; border: 1px solid #ccc }
input:focus { box-shadow: 0 0 0 4px blue; border-color: blue }
#grow { width: 10px; height: 10px; background: red; transition: width 1.5s }
#grow.big { width: 300px }
</style></head><body><input id=i><div id=grow></div></body></html>"""


def test_cosmetic_focus_ring_transition_does_not_block(browser) -> None:
    backend, page, ctx = _backend(browser, _FOCUS_RING)
    try:
        time.sleep(0.3)
        page.focus("#i")  # starts a 1.5 s box-shadow/border-color transition
        time.sleep(0.25)
        assert page.evaluate("document.getAnimations().length") > 0
        assert backend.page_quiescent(0.1) is True
    finally:
        ctx.close()


def test_layout_transition_still_blocks(browser) -> None:
    backend, page, ctx = _backend(browser, _FOCUS_RING)
    try:
        time.sleep(0.3)
        page.evaluate("document.getElementById('grow').classList.add('big')")
        time.sleep(0.25)
        assert backend.page_quiescent(0.1) is False
        assert backend._quiescence.last_reason == "busy:animation"
    finally:
        ctx.close()


class _SelectBackend:
    def __init__(self, label):
        self.label = label

    def focused_select_label_at(self, x, y):
        return self.label


class _PixelVision:
    def __init__(self):
        self.ocr_calls = 0

    def pixels_changed(self, a, b, region=None):
        return True

    def ocr(self, png, region=None):
        self.ocr_calls += 1
        return []


def _typed(label, text="Massachusetts"):
    from openadapt_flow.runtime.replayer import Replayer

    r = Replayer.__new__(Replayer)
    r.backend = _SelectBackend(label)
    r.vision = _PixelVision()
    r._settle_seeded = lambda: b"after"
    r._field_region = lambda point, region: (0, 0, 10, 10)
    return r._typed_input_landed(text, (5, 5), b"before"), r.vision.ocr_calls


def test_dropdown_typed_input_verified_from_dom() -> None:
    (landed, changed), ocr_calls = _typed("Massachusetts")
    assert landed and changed and ocr_calls == 0


@pytest.mark.parametrize("label", [None, "Maine", "Massachusetts (MA)"])
def test_dropdown_dom_miss_falls_back_and_never_passes(label) -> None:
    (landed, _changed), ocr_calls = _typed(label)
    assert landed is False  # OCR (empty here) decides; DOM miss never passes
    assert ocr_calls >= 1


def _seed_from(result):
    return settle_mod.SettleSeed(
        phash=result.phash,
        stable_frames=result.stable_frames,
        captured_at=result.captured_at,
        png=result.png,
    )


def test_unchanged_page_reuses_settled_frame(browser) -> None:
    backend, _page, ctx = _backend(browser, "<!doctype html><p>static</p>")
    try:
        time.sleep(0.3)
        first = settle_mod.wait_settled_result(backend, timeout_s=2.0)
        assert first.mode == "page" and first.phash.startswith("page:")
        shots = []
        real = backend.screenshot
        backend.screenshot = lambda: shots.append(1) or real()
        again = settle_mod.wait_settled_seeded(backend, _seed_from(first))
        assert again.settled and again.png is first.png and shots == []
    finally:
        ctx.close()


def test_any_change_since_seed_forces_a_fresh_frame(browser) -> None:
    backend, page, ctx = _backend(browser, "<!doctype html><p id=p>static</p>")
    try:
        time.sleep(0.3)
        first = settle_mod.wait_settled_result(backend, timeout_s=2.0)
        page.evaluate("document.getElementById('p').textContent = 'changed'")
        again = settle_mod.wait_settled_seeded(backend, _seed_from(first))
        assert again.settled and again.png is not first.png
        assert again.phash != first.phash
    finally:
        ctx.close()
