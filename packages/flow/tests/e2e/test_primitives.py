"""Track C — interaction-primitive taxonomy (adversarial validation).

For each interaction primitive the widgets lab page
(``openadapt_flow/mockmed/static/widgets.html?panel=<name>``) exposes one
control; each test records a tiny demonstration against it, compiles, and
replays on a fresh page. The support matrix these tests back — including
the primitives that are unsupported STRUCTURALLY (file upload,
drag-and-drop, conditional branching, multi-window) and therefore have no
test here — lives in ``docs/validation/VALIDATION.md`` and
``docs/LIMITS.md``.

Several tests are characterizations of weak behavior (vacuous successes on
steps that compiled with zero postconditions; position-based resolution of
parameterized typeahead). The wrong-row click under reorder was FIXED on
2026-07-08 (pre-click identity check) — its test now pins the safe-halt.
Comments state the desired behavior where it differs from the observed one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

from openadapt_flow.backends.playwright_backend import PlaywrightBackend
from openadapt_flow.compiler import compile_recording
from openadapt_flow.recorder import Recorder

from .validation_utils import describe, failing_step, replay_on_page

pytestmark = pytest.mark.timeout(600)

VIEWPORT = {"width": 1280, "height": 800}


def center(page, selector: str) -> tuple[int, int]:
    """Center pixel coordinates of the first element matching ``selector``.

    Record-time-only cheat, exactly like ``demo_driver.record_triage_demo``.
    """
    locator = page.locator(selector).first
    locator.wait_for(state="visible")
    box = locator.bounding_box()
    assert box is not None, f"no bounding box for {selector!r}"
    return (
        int(box["x"] + box["width"] / 2),
        int(box["y"] + box["height"] / 2),
    )


def record_and_compile(
    browser,
    url: str,
    ops: Callable[[object, Recorder], None],
    tmp_path: Path,
    name: str,
):
    """Record ``ops`` against ``url``, compile, return (workflow, dirs...)."""
    rec_dir = tmp_path / "rec"
    bundle_dir = tmp_path / "bundle"
    page = browser.new_page(viewport=VIEWPORT, device_scale_factor=1)
    try:
        page.goto(url)
        recorder = Recorder(PlaywrightBackend(page), rec_dir, app_url=url)
        ops(page, recorder)
        recorder.finish()
        recorded_status = page.locator("#status").text_content()
    finally:
        page.close()
    workflow = compile_recording(rec_dir, bundle_dir, name=name)
    return workflow, bundle_dir, recorded_status


class TestNativeSelect:
    def test_arrow_key_selection_is_faithfully_replayed_even_when_inert(
        self, mockmed_url, _browser, tmp_path
    ) -> None:
        """PARTIAL / HAZARD. A native <select> driven by click + ArrowDown +
        Enter. The dropdown popup is browser chrome — it never appears in
        page screenshots — and whether the arrow keys change the value at
        all is platform-dependent (observed inert on macOS headless
        chromium). The replay faithfully reproduces whatever the recording
        did; when the recording did NOTHING, the steps compile with zero
        postconditions (no visual change AND no structural change — the
        URL/title/tab-count fallback has nothing to assert either) and the
        replay is a VACUOUS success. Still a known limit: docs/LIMITS.md."""
        url = f"{mockmed_url}widgets.html?panel=select"

        def ops(page, r):
            r.click(*center(page, "#pet"))
            r.press("ArrowDown")
            r.press("Enter")

        wf, bundle_dir, recorded = record_and_compile(
            _browser, url, ops, tmp_path, "select-arrows"
        )
        report, state = replay_on_page(
            _browser, bundle_dir, url, tmp_path / "run", params={}
        )
        assert report.success is True, describe(report, state)
        assert state["status"] == recorded, describe(report, state)
        if recorded == "Ready and waiting.":  # the select never changed
            # Nothing changed on screen, so nothing was asserted: the
            # compiled workflow cannot detect that it does nothing.
            assert all(len(s.expect) == 0 for s in wf.steps)

    def test_type_prefix_fallback_selects_an_option(
        self, mockmed_url, _browser, tmp_path
    ) -> None:
        """SUPPORTED (workaround). click + type 'd' + Enter drives the
        select through its type-to-select behavior — the keyboard fallback
        predicted in docs/showcase-openemr/FINDINGS.md, confirmed here."""
        url = f"{mockmed_url}widgets.html?panel=select"

        def ops(page, r):
            r.click(*center(page, "#pet"))
            r.type_text("d")
            r.press("Enter")

        _wf, bundle_dir, recorded = record_and_compile(
            _browser, url, ops, tmp_path, "select-prefix"
        )
        assert recorded == "Species set to Dog."
        report, state = replay_on_page(
            _browser, bundle_dir, url, tmp_path / "run", params={}
        )
        assert report.success is True, describe(report, state)
        assert state["status"] == "Species set to Dog.", describe(report, state)


class TestCheckboxRadio:
    def test_checkbox_and_radio_clicks_replay(
        self, mockmed_url, _browser, tmp_path
    ) -> None:
        """SUPPORTED. Plain clicks on a checkbox and a radio button."""
        url = f"{mockmed_url}widgets.html?panel=checks"

        def ops(page, r):
            r.click(*center(page, "#consent"))
            r.click(*center(page, "#prio-urgent"))

        _wf, bundle_dir, recorded = record_and_compile(
            _browser, url, ops, tmp_path, "checks"
        )
        assert recorded == "Consent yes, priority Urgent."
        report, state = replay_on_page(
            _browser, bundle_dir, url, tmp_path / "run", params={}
        )
        assert report.success is True, describe(report, state)
        assert state["status"] == recorded


class TestDateInput:
    def test_typed_date_halts_when_readback_cannot_verify(
        self, mockmed_url, _browser, tmp_path
    ) -> None:
        """PARTIAL / HAZARD, shape changed 2026-07-09. Typing digits into a
        native date input is segment-, locale-, and PLATFORM-dependent: on
        the macOS renderer '07082026' produced the value 70820-02-06 AT
        RECORD TIME (and the original typed-input rule replayed the same
        garbage byte-for-byte); on the Linux CI renderer the digits do not
        register at all and the widget stays empty. Under the hardened
        verification, exact DOM value readback is authoritative on the
        browser backend. Both the transformed-value and ignored-input shapes
        SAFE-HALT at the type step because neither equals the intended digits.
        Pixel-only masked acceptance is reserved for steps explicitly declared
        secret, so an ordinary date field can no longer verify vacuously. The
        calendar popup alternative remains browser chrome that vision never
        sees."""
        url = f"{mockmed_url}widgets.html?panel=date"

        def ops(page, r):
            r.click(*center(page, "#when"))
            r.type_text("07082026")

        _wf, bundle_dir, recorded = record_and_compile(
            _browser, url, ops, tmp_path, "date"
        )
        # Platform-dependent record-time outcome: transformed garbage
        # (macOS-shape) or ignored input (Linux-shape) — never the typed
        # digits verbatim.
        assert "70820-02-06" in recorded or recorded == "Ready and waiting."
        report, state = replay_on_page(
            _browser, bundle_dir, url, tmp_path / "run", params={}
        )
        # Both platform shapes refuse: transformed garbage is an exact value
        # mismatch; ignored input remains empty through the single guarded
        # retry. Neither can be mistaken for a masked secret field.
        assert report.success is False, describe(report, state)
        failed = failing_step(report)
        assert failed is not None
        assert "Typed input could not be verified" in (failed.error or "")


class TestModalDialog:
    def test_open_and_confirm_dom_modal(self, mockmed_url, _browser, tmp_path) -> None:
        """SUPPORTED. DOM-rendered modal open + confirm are ordinary clicks."""
        url = f"{mockmed_url}widgets.html?panel=modal"

        def ops(page, r):
            r.click(*center(page, "#open-survey"))
            r.click(*center(page, "#confirm-survey"))

        _wf, bundle_dir, recorded = record_and_compile(
            _browser, url, ops, tmp_path, "modal"
        )
        assert recorded == "Survey response recorded."
        report, state = replay_on_page(
            _browser, bundle_dir, url, tmp_path / "run", params={}
        )
        assert report.success is True, describe(report, state)
        assert state["status"] == recorded


class TestTypeahead:
    def test_fixed_value_suggestion_click_replays(
        self, mockmed_url, _browser, tmp_path
    ) -> None:
        """SUPPORTED (fixed value). Type a prefix, click a suggestion."""
        url = f"{mockmed_url}widgets.html?panel=typeahead"

        def ops(page, r):
            r.click(*center(page, "#q"))
            r.type_text("Al")
            r.click(*center(page, "#suggestions button >> nth=0"))

        _wf, bundle_dir, recorded = record_and_compile(
            _browser, url, ops, tmp_path, "typeahead"
        )
        assert recorded == "Contact chosen: Alice Anders."
        report, state = replay_on_page(
            _browser, bundle_dir, url, tmp_path / "run", params={}
        )
        assert report.success is True, describe(report, state)
        assert state["status"] == recorded

    def test_parameterized_prefix_resolves_suggestion_by_position(
        self, mockmed_url, _browser, tmp_path
    ) -> None:
        """PARTIAL / HAZARD (characterization). The typed prefix is a
        workflow parameter; replaying with 'Bo' makes different suggestions
        appear. The recorded suggestion anchor ('Alice Anders') cannot
        match, so resolution falls to the geometry rung and clicks
        WHATEVER sits at the first-suggestion position — correct here by
        coincidence of intent, unverified by construction (the compiler
        also excluded the status text from postconditions because it
        embeds the parameter). Observed on macOS: picks 'Bob Baker' and
        reports success. A halt before the click is the other acceptable
        pin (platform OCR differences decide which)."""
        url = f"{mockmed_url}widgets.html?panel=typeahead"

        def ops(page, r):
            r.click(*center(page, "#q"))
            r.type_text("Al", param="prefix")
            r.click(*center(page, "#suggestions button >> nth=0"))

        _wf, bundle_dir, _recorded = record_and_compile(
            _browser, url, ops, tmp_path, "typeahead-param"
        )
        report, state = replay_on_page(
            _browser,
            bundle_dir,
            url,
            tmp_path / "run",
            params={"prefix": "Bo"},
        )
        # It must never claim the recorded contact was chosen.
        assert state["status"] != "Contact chosen: Alice Anders.", describe(
            report, state
        )
        if report.success:
            # Position-based click on the first 'Bo' suggestion.
            assert state["status"] == "Contact chosen: Bob Baker.", describe(
                report, state
            )
        else:
            # Or it halted before clicking anything.
            assert state["status"] == "Ready and waiting.", describe(report, state)


class TestTablePagination:
    def test_recorded_pagination_clicks_replay(
        self, mockmed_url, _browser, tmp_path
    ) -> None:
        """SUPPORTED (as recorded). 'Next' then pick a row on page 2. Note
        the limit: pagination must be DEMONSTRATED; if data growth moves
        the target onto another page at replay time, no recorded step
        exists to reach it (no conditional control flow)."""
        url = f"{mockmed_url}widgets.html?panel=table"

        def ops(page, r):
            r.click(*center(page, "#next-page"))
            r.click(*center(page, ".pick-btn >> nth=0"))

        _wf, bundle_dir, recorded = record_and_compile(
            _browser, url, ops, tmp_path, "pagination"
        )
        assert recorded == "Order picked: Dermatology consult."
        report, state = replay_on_page(
            _browser, bundle_dir, url, tmp_path / "run", params={}
        )
        assert report.success is True, describe(report, state)
        assert state["status"] == recorded


class TestSortReorder:
    def test_reordered_rows_halt_before_any_click(
        self, mockmed_url, _browser, tmp_path
    ) -> None:
        """FIXED (was: wrong-action then halt — the replay CLICKED the
        wrong row, writing 'Order picked: Echocardiogram.' into app state
        before the postcondition stopped the run). Record picking the
        second row (ascending order), replay against ?presort=desc where
        every row moved. Identical 'Pick' buttons still defeat the template
        rung's discrimination (row text sits mostly outside the crop), but
        the pre-click identity check compares the resolved row's band text
        against the recorded row and halts BEFORE anything is clicked — no
        state is written at all."""
        url = f"{mockmed_url}widgets.html?panel=table"

        def ops(page, r):
            r.click(*center(page, ".pick-btn >> nth=1"))

        _wf, bundle_dir, recorded = record_and_compile(
            _browser, url, ops, tmp_path, "sort-reorder"
        )
        assert recorded == "Order picked: Basic metabolic panel."
        report, state = replay_on_page(
            _browser,
            bundle_dir,
            url + "&presort=desc",
            tmp_path / "run",
            params={},
        )
        assert report.success is False, describe(report, state)
        # No click fired: app state untouched.
        assert state["status"] == "Ready and waiting.", describe(report, state)
        failed = failing_step(report)
        assert failed is not None and failed.step_id == "step_000"
        assert "Identity check failed" in (failed.error or ""), describe(report, state)


class TestKeyboardFlow:
    def test_tab_type_enter_flow_replays(self, mockmed_url, _browser, tmp_path) -> None:
        """SUPPORTED. One focusing click, then keyboard only (type, Tab,
        type, Enter)."""
        url = f"{mockmed_url}widgets.html?panel=kbd"

        def ops(page, r):
            r.click(*center(page, "#kb-name"))
            r.type_text("Rivera")
            r.press("Tab")
            r.type_text("North")
            r.press("Enter")

        _wf, bundle_dir, recorded = record_and_compile(
            _browser, url, ops, tmp_path, "kbd"
        )
        assert recorded == "Request submitted for Rivera on ward North."
        report, state = replay_on_page(
            _browser, bundle_dir, url, tmp_path / "run", params={}
        )
        assert report.success is True, describe(report, state)
        assert state["status"] == recorded


class TestNewTab:
    def test_target_blank_link_is_verified_structurally(
        self, mockmed_url, _browser, tmp_path
    ) -> None:
        """FIXED (was: vacuous success with zero postconditions). Clicking
        a target=_blank link opens a tab the single-page frame never shows;
        the recorded before/after frames are identical, so nothing visual
        can be asserted. The recorder now captures the backend's structural
        observations, and the compiler mines a NEW_TAB_OPENED fallback
        postcondition — the step passes only if a tab actually opened, and
        would fail if the click stopped working. (What happens INSIDE the
        new tab remains unobserved: single-window scope, docs/LIMITS.md.)"""
        from openadapt_flow.ir import PostconditionKind

        url = f"{mockmed_url}widgets.html?panel=newtab"

        def ops(page, r):
            r.click(*center(page, "#report-link"))

        wf, bundle_dir, recorded = record_and_compile(
            _browser, url, ops, tmp_path, "newtab"
        )
        assert recorded == "Ready and waiting."  # original page unchanged
        kinds = [pc.kind for s in wf.steps for pc in s.expect]
        assert PostconditionKind.NEW_TAB_OPENED in kinds, wf.steps
        report, state = replay_on_page(
            _browser, bundle_dir, url, tmp_path / "run", params={}
        )
        assert report.success is True, describe(report, state)
        assert report.results[0].postconditions_ok is True
