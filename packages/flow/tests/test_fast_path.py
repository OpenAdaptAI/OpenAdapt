"""Browser fast path: native fill, the off-screen probe, and step timings."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from openadapt_flow.ir import StructuralLocator
from openadapt_flow.runtime import step_timing

_FORM = """<!doctype html><html><body style="margin:0">
<script>window.keys = []; document.addEventListener('keydown', e => window.keys.push(e.key));
window.inputs = 0; document.addEventListener('input', () => { window.inputs += 1; });</script>
<input id=plain value="">
<input id=combo role=combobox aria-autocomplete=list value="">
<input id=dated type=date>
<div style="height:3000px"></div>
<input id=far value="">
</body></html>"""


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


@pytest.fixture()
def form(browser):
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    ctx = browser.new_context(viewport={"width": 800, "height": 600})
    page = ctx.new_page()
    page.set_content(_FORM)
    try:
        yield PlaywrightBackend(page, native_fill=True), page
    finally:
        ctx.close()


def test_plain_text_field_gets_one_insert_with_input_events(form) -> None:
    backend, page = form
    page.focus("#plain")
    backend.type_text("Nadia Brennan")
    assert page.input_value("#plain") == "Nadia Brennan"
    assert page.evaluate("window.inputs") >= 1  # the app's input handler ran
    assert page.evaluate("window.keys") == []  # no per-key events


def test_widgets_and_opt_out_type_key_by_key(form) -> None:
    backend, page = form
    page.focus("#combo")
    backend.type_text("ab")
    assert page.input_value("#combo") == "ab"
    assert page.evaluate("window.keys") == ["a", "b"]
    backend.native_fill = False
    page.focus("#plain")
    backend.type_text("cd")
    assert page.input_value("#plain") == "cd"
    assert page.evaluate("window.keys") == ["a", "b", "c", "d"]


def test_insert_replaces_a_selection_like_typing(form) -> None:
    backend, page = form
    page.fill("#plain", "old value")
    page.focus("#plain")
    page.keyboard.press("ControlOrMeta+a")
    backend.type_text("new")
    assert page.input_value("#plain") == "new"


def test_offscreen_probe(form) -> None:
    backend, _page = form
    assert backend.structural_offscreen(StructuralLocator(selector="#far")) is True
    # On screen, missing, or ambiguous: not shown to be off-screen.
    assert backend.structural_offscreen(StructuralLocator(selector="#plain")) is None
    assert backend.structural_offscreen(StructuralLocator(selector="#nope")) is None
    assert backend.structural_offscreen(StructuralLocator(selector="input")) is None


def test_step_timer_spans_are_exclusive_and_add_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A fake clock: each span "spends" exact milliseconds, so the bounds are
    # exact instead of depending on how long sleep() takes on a loaded host.
    now = [0.0]
    monkeypatch.setattr(
        step_timing, "time", SimpleNamespace(perf_counter=lambda: now[0])
    )

    def spend(ms: float) -> None:
        now[0] += ms / 1000.0

    @step_timing.timed_span("resolve")
    def resolve():
        spend(20)
        identity()

    @step_timing.timed_span("identity")
    def identity():
        spend(30)

    @step_timing.timed_span("actuate")
    def act():
        spend(5)
        settle()

    @step_timing.timed_span("settle")
    def settle():
        spend(10)

    settle()  # untimed: no step running
    timer, prev = step_timing.start_step()
    settle()
    resolve()
    act()
    spend(7)  # step time outside every span
    step_timing.end_step(prev)
    out = timer.result_ms()
    # Exclusive: resolve does not include the identity call nested in it, and
    # actuate does not include its settle.
    assert out == pytest.approx(
        {
            "settle_before": 10.0,
            "resolve": 20.0,
            "identity": 30.0,
            "actuate": 5.0,
            "settle_after": 10.0,
            "other": 7.0,
        }
    )
    # The spans add up to the step's elapsed time.
    assert sum(out.values()) == pytest.approx((now[0] - timer._t0) * 1000)
    assert step_timing.current() is prev


_PICKER = """<!doctype html><html><body style="margin:0">
<div id=picker><span>October</span>
<div style="display:none"><div>January</div><div>February</div></div>
<span>2026</span></div></body></html>"""


def test_visible_text_ignores_hidden_option_lists(browser) -> None:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    ctx = browser.new_context(viewport={"width": 800, "height": 600})
    page = ctx.new_page()
    page.set_content(_PICKER)
    try:
        backend = PlaywrightBackend(page)
        assert backend.visible_text_present("October-2026-") is True
        assert backend.visible_text_present("JanuaryFebruary") is None  # hidden
    finally:
        ctx.close()


def test_native_fill_is_off_unless_the_fast_profile_is_on(browser, monkeypatch) -> None:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.runtime.speed import apply_speed_profile, current_speed_settings
    from openadapt_flow.vision import settle

    monkeypatch.delenv("OPENADAPT_FLOW_NATIVE_FILL", raising=False)
    monkeypatch.setattr(settle, "PAGE_QUIET_WINDOW_S", None)
    ctx = browser.new_context()
    page = ctx.new_page()
    try:
        assert PlaywrightBackend(page).native_fill is False
        monkeypatch.setenv("OPENADAPT_FLOW_SPEED", "standard")
        assert apply_speed_profile("fast")["page_quiet_ms"] == 50
        assert PlaywrightBackend(page).native_fill is True
        assert current_speed_settings()["ocr_threads"] == 2
        apply_speed_profile("standard")
        assert PlaywrightBackend(page).native_fill is False
        assert current_speed_settings() == {
            "speed_profile": "standard",
            "native_fill": False,
            "page_quiet_ms": 100,
            "ocr_threads": None,
        }
    finally:
        ctx.close()
        monkeypatch.delenv("OPENADAPT_FLOW_OCR_THREADS", raising=False)


_NUMBERS = """<!doctype html><html><body style="margin:0">
<p>Balance 1,000.00 due</p><p>Claim 12-345</p><p>Delta -5 units</p></body></html>"""


def test_number_separators_still_count(browser) -> None:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    ctx = browser.new_context(viewport={"width": 800, "height": 600})
    page = ctx.new_page()
    page.set_content(_NUMBERS)
    try:
        backend = PlaywrightBackend(page)
        assert backend.visible_text_present("1,000.00") is True
        assert backend.visible_text_present("100,000") is None
        assert backend.visible_text_present("12-345") is True
        assert backend.visible_text_present("123-45") is None
        assert backend.visible_text_present("Delta -5") is True
        assert backend.visible_text_present("Delta 5") is None
    finally:
        ctx.close()
