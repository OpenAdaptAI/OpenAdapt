"""A JavaScript dialog is a screen state answered by a backend action.

A ``confirm()`` dialog is not a surface. The step whose click raises it
carries the answer (``Step.dialog``) and the dialog's message as its identity.
The browser backend answers it only when the message matches; a different
message is dismissed, never accepted, and the run halts
(docs/MULTI_SURFACE.md, "Dialogs").
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")

import tests.test_multi_surface_e2e as _e2e  # noqa: E402
from openadapt_flow.backends.playwright_backend import PlaywrightBackend  # noqa: E402
from openadapt_flow.compiler import compile_recording  # noqa: E402
from openadapt_flow.ir import ActionKind, DialogResponse, Step, Workflow  # noqa: E402
from openadapt_flow.recorder import Recorder  # noqa: E402

pytestmark = pytest.mark.timeout(600)

app_url = _e2e.app_url
browser = _e2e.browser


@pytest.fixture(scope="module")
def dialog_bundle(app_url, browser, tmp_path_factory) -> tuple[Path, Workflow]:
    root = tmp_path_factory.mktemp("dialog")
    url = app_url + "/confirm.html"
    page = _e2e._fresh_page(browser, url)
    try:
        recorder = Recorder(
            PlaywrightBackend(page),
            root / "recording",
            app_url=url,
            settle_interval_s=0.05,
            settle_timeout_s=2.0,
        )
        recorder.expect_dialog("accept")
        recorder.click(*_e2e._center(page, "#delete"))
        assert page.locator("#record-status").inner_text() == "Record Deleted"
        recording = recorder.finish()
    finally:
        page.close()
    workflow = compile_recording(recording, root / "bundle", name="dialog")
    return root / "bundle", workflow


def test_the_dialog_answer_is_compiled_onto_the_step_that_raises_it(
    dialog_bundle,
) -> None:
    _bundle_dir, workflow = dialog_bundle
    assert workflow.surfaces == []  # a dialog is not a surface
    [step] = workflow.steps
    assert step.action is ActionKind.CLICK
    assert step.dialog == DialogResponse(response="accept", message="Delete record 42?")


def test_a_matching_dialog_is_answered(dialog_bundle, app_url, browser, tmp_path):
    bundle_dir, _workflow = dialog_bundle
    report, page = _e2e._replay(
        browser, app_url + "/confirm.html", bundle_dir, tmp_path / "run"
    )
    try:
        assert report.success, _e2e._summary(report)
        assert report.model_calls == 0
        assert page.locator("#record-status").inner_text() == "Record Deleted"
    finally:
        page.close()


def test_a_dialog_identity_mismatch_halts_and_is_dismissed(
    dialog_bundle, app_url, browser, tmp_path
):
    bundle_dir, _workflow = dialog_bundle
    report, page = _e2e._replay(
        browser,
        app_url + "/confirm.html?msg=Delete%20ALL%20records%3F",
        bundle_dir,
        tmp_path / "run",
    )
    try:
        assert not report.success
        [result] = report.results
        assert not result.ok
        assert "a browser dialog showed 'Delete ALL records?'" in str(result.error)
        assert "not the recorded 'Delete record 42?'" in str(result.error)
        # Dismissed, never accepted: nothing was deleted.
        assert page.locator("#record-status").inner_text() == "Record Kept"
    finally:
        page.close()


def test_a_screen_driven_backend_refuses_a_dialog_step() -> None:
    class _Pixels:
        pass

    from openadapt_flow.runtime.replayer import Replayer

    replayer = Replayer.__new__(Replayer)
    replayer.backend = _Pixels()  # type: ignore[assignment]
    step = Step(
        id="s",
        intent="delete",
        action=ActionKind.KEY,
        key="Enter",
        dialog=DialogResponse(response="accept", message="Sure?"),
    )
    error = Replayer._act(replayer, step, None, {})
    assert error is not None and "cannot see browser dialogs" in error


def test_a_wait_step_cannot_carry_a_dialog() -> None:
    with pytest.raises(ValueError, match="raises no dialog"):
        Step(
            id="s",
            intent="wait",
            action=ActionKind.WAIT,
            dialog=DialogResponse(response="accept", message="Sure?"),
        )
