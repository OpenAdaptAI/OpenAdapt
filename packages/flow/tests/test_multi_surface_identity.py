"""Surfaces bound by identity, not handles (docs/MULTI_SURFACE.md).

Uses the popup + tab fixture app and bundle from test_multi_surface_e2e.py.

- A durable run that pauses on the report tab resumes in a NEW Replayer: the
  checkpoint carries surface names and last-seen observations, never page
  handles, and the resume re-binds the tab by the step's identity check. The
  pause after a consequential click is a "Check the record" stop, and a resume
  still needs the operator's approval.
- A second page that passes the same identity check makes the re-bind
  ambiguous: the run halts and drives neither page.
- A page that no longer passes the identity check is a routing mismatch: the
  run halts instead of acting on it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")

import tests.test_multi_surface_e2e as _e2e  # noqa: E402
from openadapt_flow.backends.playwright_backend import PlaywrightBackend  # noqa: E402
from openadapt_flow.ir import Workflow  # noqa: E402
from openadapt_flow.runtime.durable import (  # noqa: E402
    ApprovalRequired,
    CheckpointStore,
    StateDiverged,
    resume,
)
from openadapt_flow.runtime.effects import Effect, EffectKind  # noqa: E402
from openadapt_flow.runtime.replayer import Replayer  # noqa: E402
from tests.test_durable_runtime import (  # noqa: E402
    FakeSoRVerifier,
    _retry_approval,
)

pytestmark = pytest.mark.timeout(600)

app_url = _e2e.app_url
browser = _e2e.browser
bundle = _e2e.bundle

#: step_006 clicks "Mark Reviewed" on the report tab (surface tab_1).
PAUSE_STEP = "step_006"


def _durable_bundle(bundle, tmp_path: Path) -> Path:
    """The e2e bundle with a system-of-record effect on the report-tab step."""

    source, _ = bundle
    target = tmp_path / "bundle"
    shutil.copytree(source, target)
    workflow = Workflow.load(target)
    steps = [
        step.model_copy(
            update={
                "effects": [
                    Effect(
                        kind=EffectKind.RECORD_WRITTEN,
                        match={"step": step.id},
                        expected_count=1,
                        timeout_s=0.5,
                    )
                ],
                "risk": "irreversible",
            }
        )
        if step.id == PAUSE_STEP
        else step
        for step in workflow.steps
    ]
    workflow.model_copy(update={"steps": steps}).save(target)
    return target


def _pause_on_the_tab(browser, app_url, bundle, tmp_path):
    bundle_dir = _durable_bundle(bundle, tmp_path)
    run_dir = tmp_path / "run"
    verifier = FakeSoRVerifier()
    verifier.refute.add((("step", PAUSE_STEP),))
    page = _e2e._fresh_page(browser, app_url)
    report = Replayer(
        PlaywrightBackend(page),
        durable=True,
        effect_verifier=verifier,
        poll_interval_s=0.05,
    ).run(Workflow.load(bundle_dir), bundle_dir=bundle_dir, run_dir=run_dir)
    return report, page, bundle_dir, run_dir, verifier


def test_resume_rebinds_surfaces_by_identity(bundle, app_url, browser, tmp_path):
    report, page, bundle_dir, run_dir, verifier = _pause_on_the_tab(
        browser, app_url, bundle, tmp_path
    )
    try:
        assert not report.success, _e2e._summary(report)
        assert report.results[-1].step_id == PAUSE_STEP
        # A consequential click whose save was not confirmed: a person checks
        # the record before anything is retried.
        assert report.transaction_outcome == "RECONCILIATION_REQUIRED"

        store = CheckpointStore(run_dir)
        last = store.last_checkpoint()
        assert last is not None and last.step_id == "step_005"
        # Names and observations, never handles.
        assert last.active_surface == "tab_1"
        assert last.surface == "tab_1"
        assert set(last.surface_observations) == {"main", "popup_1", "tab_1"}
        tab_seen = last.surface_observations["tab_1"]
        assert tab_seen.origin == app_url.lower() and tab_seen.screen_phash
        # An observation holds no URL path, title, screen text, or handle.
        assert set(tab_seen.model_dump()) == {
            "surface",
            "kind",
            "observed_after_step",
            "origin",
            "screen_phash",
        }

        verifier.refute.clear()
        fresh = Replayer(
            PlaywrightBackend(page), effect_verifier=verifier, poll_interval_s=0.05
        )
        # The "Check the record" stop: no resume without the operator.
        with pytest.raises(ApprovalRequired):
            resume(run_dir, fresh, approval=None)

        resumed = resume(run_dir, fresh, approval=_retry_approval(bundle_dir, run_dir))
        assert resumed.success, _e2e._summary(resumed)
        assert resumed.model_calls == 0
        by_id = {r.step_id: r for r in resumed.results}
        assert by_id[PAUSE_STEP].surface == "tab_1"
        assert by_id["step_008"].surface == "main"
        assert page.locator("#done").inner_text() == "Referral Finished"
    finally:
        page.context.close()


def test_ambiguous_rebind_on_resume_halts(bundle, app_url, browser, tmp_path):
    report, page, bundle_dir, run_dir, verifier = _pause_on_the_tab(
        browser, app_url, bundle, tmp_path
    )
    try:
        assert not report.success
        # A second report tab that looks exactly like the first.
        with page.expect_popup() as extra_info:
            page.click("#open-report")
        extra = extra_info.value
        extra.wait_for_load_state("load")
        verifier.refute.clear()
        fresh = Replayer(
            PlaywrightBackend(page), effect_verifier=verifier, poll_interval_s=0.05
        )
        with pytest.raises(StateDiverged, match="2 open browser pages pass"):
            resume(run_dir, fresh, approval=_retry_approval(bundle_dir, run_dir))
        # Neither report tab, nor the main page, was driven.
        assert extra.locator("#ack").inner_text() == ""
        assert page.locator("#done").inner_text() == ""
        assert CheckpointStore(run_dir).read_pending() is not None
    finally:
        page.context.close()


def test_page_routing_mismatch_halts(bundle, app_url, browser, tmp_path):
    report, page, bundle_dir, run_dir, verifier = _pause_on_the_tab(
        browser, app_url, bundle, tmp_path
    )
    try:
        assert not report.success
        # The only candidate for the report tab now shows another document.
        report_tab = page.context.pages[2]
        report_tab.goto(f"{app_url}/promo.html")
        verifier.refute.clear()
        fresh = Replayer(
            PlaywrightBackend(page), effect_verifier=verifier, poll_interval_s=0.05
        )
        with pytest.raises(StateDiverged, match="no open browser page passes"):
            resume(run_dir, fresh, approval=_retry_approval(bundle_dir, run_dir))
        assert page.locator("#done").inner_text() == ""
    finally:
        page.context.close()


def test_step_routing_binds_only_the_page_that_passes_its_identity(
    bundle, app_url, browser
):
    """Per-step routing inside the browser, without a checkpoint."""

    from openadapt_flow.runtime.surfaces import SurfaceRouter

    bundle_dir, workflow = bundle
    page = _e2e._fresh_page(browser, app_url)
    try:
        with page.expect_popup() as info:
            page.click("#open-report")
        report_tab = info.value
        report_tab.wait_for_load_state("load")
        replayer = Replayer(PlaywrightBackend(page))
        replayer._surface_identity_context = (workflow, bundle_dir)
        step = next(s for s in workflow.steps if s.id == PAUSE_STEP)

        router = SurfaceRouter(
            workflow,
            PlaywrightBackend(page),
            identity_check=replayer._surface_identity_check,
            resume_active="tab_1",
        )
        assert router.route(step) is None
        assert router.active_backend.page is report_tab

        report_tab.goto(f"{app_url}/promo.html")
        mismatch = SurfaceRouter(
            workflow,
            PlaywrightBackend(page),
            identity_check=replayer._surface_identity_check,
            resume_active="tab_1",
        )
        error = mismatch.route(step)
        assert error is not None and "no open browser page passes" in error
        assert mismatch.active_backend is None
    finally:
        page.context.close()
