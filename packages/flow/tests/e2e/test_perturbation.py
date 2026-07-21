"""Track A — perturbation/drift matrix (adversarial validation).

Replays the SAME compiled bundle (recorded at 1280x800, default MockMed)
under one perturbation at a time and pins down what actually happens. These
are CHARACTERIZATION tests: several assert on known failure modes so that
any change in behavior (fix or regression) is caught loudly. The silent
wrong-patient writes originally found here were FIXED on 2026-07-08 by the
pre-click identity check (runtime.identity); the data-drift tests now pin
the safe-halt behavior. The experiment matrix, severity ranking, and
evidence pointers live in ``docs/validation/VALIDATION.md``.

Outcome vocabulary used in comments below (see VALIDATION.md):

- pass:         run succeeded and did what the demonstration did.
- safe-halt:    run stopped with an accurate report; no further actions.
- false-abort:  safe-halt whose cause was cosmetic, not semantic.
- wrong-action: an action executed on the wrong target, or wrong state
  written. THE critical class. Some wrong-actions below end in a reported
  SUCCESS — those are the headline findings.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .conftest import PARAMS, drift_url
from .validation_utils import describe, failing_step, replay_on_page

pytestmark = pytest.mark.timeout(600)


@pytest.fixture()
def run_dir(tmp_path: Path) -> Path:
    return tmp_path / "run"


class TestViewportChange:
    """MockMed's layout is left-anchored with max-width 960, so growing the
    window does not move anything: this pins the (unearned) easy case."""

    @pytest.mark.parametrize("viewport", [(1440, 900), (1024, 768)])
    def test_larger_and_smaller_viewports_pass(
        self, bundle, mockmed_url, _browser, run_dir, viewport
    ) -> None:
        report, state = replay_on_page(
            _browser,
            bundle.dir,
            mockmed_url,
            run_dir,
            params=dict(PARAMS),
            viewport=viewport,
        )
        assert report.success, describe(report, state)
        assert report.heal_count == 0
        assert state["hash"] == "#patient/p1"

    def test_short_viewport_halts_without_saving(
        self, bundle, mockmed_url, _browser, run_dir
    ) -> None:
        """900x360 leaves the Save button below the fold. The workflow
        recorded no scrolls, so the replayer has no recorded gesture to
        adapt (closed-loop scrolling only extends RECORDED scroll steps) —
        the run must halt without saving anything. Observed: a FALSE-ABORT
        arrives even earlier, at the note-field click, because that step's
        REGION_STABLE region extends below the smaller viewport."""
        report, state = replay_on_page(
            _browser,
            bundle.dir,
            mockmed_url,
            run_dir,
            params=dict(PARAMS),
            viewport=(900, 360),
        )
        assert report.success is False, describe(report, state)
        assert state["banner"] is None  # nothing was saved
        failed = failing_step(report)
        assert failed is not None
        assert failed.step_id in ("step_008", "step_010"), describe(report, state)


class TestScaleChanges:
    """Device-scale-factor and CSS zoom perturbations.

    Both defeat every resolution rung's assumptions (the template scale
    ladder tops out at 1.18x; OCR/landmark coordinates come back in frame
    pixels that no longer equal input pixels under dsf=2). The safety net
    holds — postconditions abort the run at the FIRST step — but a purely
    cosmetic 125% zoom rendering the workflow 0% replayable is an
    availability failure worth stating plainly."""

    def test_device_scale_factor_2_halts_at_first_step(
        self, bundle, mockmed_url, _browser, run_dir
    ) -> None:
        report, state = replay_on_page(
            _browser,
            bundle.dir,
            mockmed_url,
            run_dir,
            params=dict(PARAMS),
            device_scale_factor=2,
        )
        assert report.success is False, describe(report, state)
        assert state["banner"] is None
        failed = failing_step(report)
        assert failed is not None
        assert failed.step_id == "step_000", describe(report, state)

    def test_css_zoom_125_halts_at_first_step(
        self, bundle, mockmed_url, _browser, run_dir
    ) -> None:
        report, state = replay_on_page(
            _browser,
            bundle.dir,
            drift_url(mockmed_url, "zoom"),
            run_dir,
            params=dict(PARAMS),
        )
        assert report.success is False, describe(report, state)
        assert state["banner"] is None
        failed = failing_step(report)
        assert failed is not None
        assert failed.step_id == "step_000", describe(report, state)


class TestFontDrift:
    def test_font_size_bump_halts_at_first_step(
        self, bundle, mockmed_url, _browser, run_dir
    ) -> None:
        """A user-side font-size preference (16px -> 19px) reflows text and
        shifts layout. Unlike theme drift (which the README showcases as
        fully healed), font drift FALSE-ABORTS at the very first step: the
        REGION_STABLE postcondition's phash cannot tolerate reflowed glyph
        metrics, and its stored template crop no longer matches either.
        Cosmetic drift, zero replayability."""
        report, state = replay_on_page(
            _browser,
            bundle.dir,
            drift_url(mockmed_url, "font"),
            run_dir,
            params=dict(PARAMS),
        )
        assert report.success is False, describe(report, state)
        assert state["banner"] is None
        failed = failing_step(report)
        assert failed is not None
        assert failed.step_id == "step_000", describe(report, state)
        assert failed.postconditions_ok is False


class TestDataDrift:
    """Rows added/removed between recording and replay — the previously
    dangerous quadrant. The discriminative evidence for a table row's
    button (the patient's NAME) sits outside the 160x64 template crop, so
    until 2026-07-08 all three cases below SAVED THE ENCOUNTER TO THE
    WRONG PATIENT with a green report (see VALIDATION.md history). The
    pre-click identity check (runtime.identity: the recorded row's context
    band must match the live band around the resolved point) now turns
    every wrong-row resolution into a safe halt BEFORE the click."""

    def test_lookalike_row_above_target_safe_halts(
        self, bundle, mockmed_url, _browser, run_dir
    ) -> None:
        """FIXED (was: wrong-action, silent — saved to '#patient/p0'). A
        new referral with the same reason and priority as the target lands
        directly above it: its Open-button crop is pixel-identical to the
        recorded template at exactly the recorded position, so the template
        rung still matches it at confidence ~1.0 — but the identity band
        reads 'Taylor Duplicate ...' where 'Jane Sample ...' was recorded
        (coverage ~0.67 from the shared reason/priority columns, below the
        0.8 bar) and the run halts without clicking anything."""
        report, state = replay_on_page(
            _browser,
            bundle.dir,
            drift_url(mockmed_url, "lookalike"),
            run_dir,
            params=dict(PARAMS),
        )
        assert report.success is False, describe(report, state)
        assert state["hash"] == "#tasks", describe(report, state)  # no click
        assert state["banner"] is None  # nothing was saved
        failed = failing_step(report)
        assert failed is not None and failed.step_id == "step_005"
        assert "Identity check failed" in (failed.error or "")
        assert failed.identity is not None
        assert failed.identity.status == "mismatch"

    def test_missing_target_row_safe_halts(
        self, bundle, mockmed_url, _browser, run_dir
    ) -> None:
        """FIXED (was: wrong-action, silent — saved to the neighbouring
        patient '#patient/p2'). The target referral is GONE and Alex
        Testcase's row occupies the recorded position; every rung that
        fires resolves to it, but the identity band ('Alex Testcase
        Cardiology follow-up Medium') shares nothing with the recorded row
        (coverage 0.0) — safe halt, never click a look-alike."""
        report, state = replay_on_page(
            _browser,
            bundle.dir,
            drift_url(mockmed_url, "missing"),
            run_dir,
            params=dict(PARAMS),
        )
        assert report.success is False, describe(report, state)
        assert state["hash"] == "#tasks", describe(report, state)
        assert state["banner"] is None
        failed = failing_step(report)
        assert failed is not None and failed.step_id == "step_005"
        assert "Identity check failed" in (failed.error or "")
        assert failed.identity is not None
        assert failed.identity.status == "mismatch"

    def test_data_growth_never_saves_to_the_wrong_patient(
        self, bundle, mockmed_url, _browser, run_dir
    ) -> None:
        """FIXED (was: wrong-action, silent — saved to '#patient/g1', the
        imposter row at the recorded position). Four unrelated referrals
        arrive above the target. Which rung fires first is
        platform/rendering-dependent (local template on the imposter vs
        global template on the true row lower down), so BOTH safe outcomes
        are pinned: an identity halt with nothing saved (imposter resolved
        first — observed on macOS), or a success that saved to the TRUE
        patient '#patient/p1' (true row resolved first). A save to any
        other patient is the fixed failure mode."""
        report, state = replay_on_page(
            _browser,
            bundle.dir,
            drift_url(mockmed_url, "grow"),
            run_dir,
            params=dict(PARAMS),
        )
        if report.success:
            assert state["hash"] == "#patient/p1", describe(report, state)
            assert state["banner"] is not None
        else:
            assert state["hash"] == "#tasks", describe(report, state)
            assert state["banner"] is None
            failed = failing_step(report)
            assert failed is not None and failed.step_id == "step_005"
            assert "Identity check failed" in (failed.error or "")

    def test_empty_state_halts_before_reaching_the_table(
        self, bundle, mockmed_url, _browser, run_dir
    ) -> None:
        """SAFE-HALT (by luck, one step early): with no referrals at all,
        the SIGN-IN step's own postcondition — which asserts another data
        row's text ('Cardiology follow-up') as if it were an invariant —
        fails first. The halt is safe, but note the mechanism: postcondition
        text mined from mutable table DATA, not from chrome."""
        report, state = replay_on_page(
            _browser,
            bundle.dir,
            drift_url(mockmed_url, "empty"),
            run_dir,
            params=dict(PARAMS),
        )
        assert report.success is False, describe(report, state)
        assert state["banner"] is None
        failed = failing_step(report)
        assert failed is not None
        assert failed.step_id in ("step_004", "step_005"), describe(report, state)


class TestSlowApp:
    """Timeout envelope: renders delayed 4s recover (postcondition polling
    plus ladder retry absorb them); renders delayed 12s exceed the ~5.5s
    postcondition window and safe-halt with an accurate report."""

    def test_4s_render_delay_recovers(
        self, bundle, mockmed_url, _browser, run_dir
    ) -> None:
        # Timing margin is thin by design: the 4s delay must fit inside
        # the ~5.5s postcondition window (pc timeout + one re-settle), so
        # a slow CI machine adds real risk of a false halt here. If this
        # test flakes, suspect machine load before suspecting the product.
        report, state = replay_on_page(
            _browser,
            bundle.dir,
            drift_url(mockmed_url, "slow"),
            run_dir,
            params=dict(PARAMS),
        )
        assert report.success, describe(report, state)
        assert state["hash"] == "#patient/p1"

    def test_12s_render_delay_halts_safely(
        self, bundle, mockmed_url, _browser, run_dir
    ) -> None:
        report, state = replay_on_page(
            _browser,
            bundle.dir,
            drift_url(mockmed_url, "slow") + "&slowms=12000",
            run_dir,
            params=dict(PARAMS),
        )
        assert report.success is False, describe(report, state)
        assert state["banner"] is None
        failed = failing_step(report)
        assert failed is not None
        assert failed.step_id == "step_004", describe(report, state)
        assert "Postconditions failed" in (failed.error or "")
