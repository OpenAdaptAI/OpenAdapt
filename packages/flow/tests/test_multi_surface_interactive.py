"""Interactive ``record --url`` follows popups and tabs as surfaces.

The same fixture app as test_multi_surface_e2e.py: a ``window.open`` popup and
a ``target=_blank`` tab. A scripted user drives the OWNED (headless) browser
the InteractiveRecorder launched; nothing tells the recorder about the extra
pages. It must claim each page as a surface when it opens and record a
surface switch when the user acts in another page, producing the same bundle
shape the scripted Recorder produces, which then replays with zero model
calls. See docs/MULTI_SURFACE.md ("Interactive recording").
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")

import tests.test_multi_surface_e2e as _e2e  # noqa: E402
from openadapt_flow.compiler import compile_recording  # noqa: E402
from openadapt_flow.interactive_recorder import record_interactive  # noqa: E402
from openadapt_flow.ir import ActionKind, Workflow  # noqa: E402

pytestmark = pytest.mark.timeout(600)

app_url = _e2e.app_url
browser = _e2e.browser


def _drive(page, pump) -> None:
    def settle(n: int = 3) -> None:
        for _ in range(n):
            pump()

    page.wait_for_selector("#open-popup", state="visible", timeout=20000)
    with page.expect_popup() as popup_info:
        page.click("#open-popup")
    popup = popup_info.value
    popup.wait_for_load_state("load")
    settle()
    popup.click("#approve")
    settle()
    with page.expect_popup() as tab_info:
        page.click("#open-report")
    tab = tab_info.value
    tab.wait_for_load_state("load")
    settle()
    tab.click("#reviewed")
    settle()
    page.click("#finish")
    settle(5)


@pytest.fixture(scope="module")
def bundle(app_url: str, tmp_path_factory) -> tuple[Path, Workflow]:
    root = tmp_path_factory.mktemp("interactive_multi_surface")
    recording = record_interactive(
        app_url, root / "recording", headless=True, script=_drive
    )
    workflow = compile_recording(recording, root / "bundle", name="interactive-ms")
    return root / "bundle", workflow


def test_interactive_recording_has_the_scripted_bundle_shape(bundle) -> None:
    _bundle_dir, workflow = bundle
    assert [s.id for s in workflow.surfaces] == ["main", "popup_1", "tab_1"]
    popup, tab = workflow.surfaces[1], workflow.surfaces[2]
    assert popup.opened_by_step == "step_000" and popup.opener_link == "window_opener"
    assert tab.opener_link == "none"
    assert [(s.action, s.surface) for s in workflow.steps] == [
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


def test_interactive_multi_surface_bundle_replays_with_zero_model_calls(
    bundle, app_url: str, browser: object, tmp_path
) -> None:
    bundle_dir, _ = bundle
    report, page = _e2e._replay(browser, app_url, bundle_dir, tmp_path / "run")
    try:
        assert report.success, _e2e._summary(report)
        assert report.model_calls == 0
        assert page.locator("#popup-status").inner_text() == "Approval Granted"
        assert page.locator("#done").inner_text() == "Referral Finished"
        report_page = page.context.pages[2]
        assert report_page.locator("#ack").inner_text() == "Review Recorded Today"
    finally:
        page.context.close()
