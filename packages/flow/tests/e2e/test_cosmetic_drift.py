"""Cosmetic-drift operating envelope (characterization tests).

Replays the SAME compiled bundle (recorded at 1280x800, dsf=1, 16px Arial)
under cosmetic-ONLY render perturbations: browser zoom, device pixel ratio
(DPI), font-size scaling, and font-family substitution. The target is always
present and semantically identical -- only rendering changes -- so a correct
run always saves the encounter to patient ``p1`` (Jane Sample, ``#patient/p1``)
and any save to a different patient is a WRONG-ACTION.

These tests pin the envelope characterized by ``benchmark/cosmetic_drift`` and
documented in ``benchmark/cosmetic_drift/COSMETIC_DRIFT.md``:

- Deterministic replay holds ONLY at the recorded render scale/metrics:
  exactly 100% zoom, 1x DPI, exact font-size. Font-FAMILY may change to a
  proportional face and still pass (OCR + healing absorb it).
- ANY scale drift (zoom != 100%, DPI > 1x, font-size != recorded) halts SAFE
  at ``step_000`` on its ``region_stable`` postcondition -- a cosmetic
  false-abort, never a wrong write.
- The headline safety property: across the whole cosmetic sweep NO
  perturbation ever produces a wrong-action (no save to the wrong patient).

Outcome vocabulary matches ``test_perturbation.py`` / VALIDATION.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .conftest import PARAMS
from .validation_utils import describe, failing_step, replay_cosmetic

pytestmark = pytest.mark.timeout(600)

TARGET_HASH = "#patient/p1"


@pytest.fixture()
def run_dir(tmp_path: Path) -> Path:
    return tmp_path / "run"


def _no_wrong_write(state: dict) -> bool:
    """True when nothing was saved to a patient other than the target."""
    return state["banner"] is None or state["hash"] == TARGET_HASH


class TestBaselineControl:
    def test_baseline_passes_all_template(
        self, bundle, mockmed_url, _browser, run_dir
    ) -> None:
        """The un-perturbed control: every anchored step resolves on the
        strongest rung (``template``), zero heals, saved to the target."""
        report, state = replay_cosmetic(
            _browser, bundle.dir, mockmed_url, run_dir, params=dict(PARAMS)
        )
        assert report.success, describe(report, state)
        assert state["hash"] == TARGET_HASH
        assert state["banner"] is not None
        assert report.heal_count == 0
        assert report.rung_counts.get("template", 0) >= 1


class TestScaleDriftSafeHalts:
    """Every scale-type cosmetic drift halts SAFE at the first step.

    step_000 is an UNLABELED click (no ocr_text): under scale drift its
    template crop no longer matches (the 0.985 threshold tolerates almost no
    resizing) and the ladder falls through to the ``geometry`` rung, which
    still finds a plausible point -- but the step's ``region_stable``
    postcondition (structural phash + a template crop) rejects the drifted
    render and the run aborts BEFORE progressing. Availability-tight,
    fail-safe."""

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"zoom": 0.90},
            {"zoom": 1.10},
            {"zoom": 1.25},  # the deployment-blocker case
            {"zoom": 1.50},
            {"zoom": 2.00},
            {"device_scale_factor": 2},
            {"device_scale_factor": 3},
            {"font_scale": 1.10},
            {"font_scale": 1.1875},  # the 16->19px bump MockMed's drift=font uses
            {"font_scale": 1.375},
            {"zoom": 1.25, "device_scale_factor": 2},  # realistic pairing
        ],
    )
    def test_scale_drift_halts_at_step_000_without_saving(
        self, bundle, mockmed_url, _browser, run_dir, kwargs
    ) -> None:
        report, state = replay_cosmetic(
            _browser,
            bundle.dir,
            mockmed_url,
            run_dir,
            params=dict(PARAMS),
            **kwargs,
        )
        assert report.success is False, describe(report, state)
        assert state["banner"] is None, describe(report, state)  # no save
        assert _no_wrong_write(state), describe(report, state)
        failed = failing_step(report)
        assert failed is not None
        assert failed.step_id == "step_000", describe(report, state)
        # Cosmetic halt at the first step: either the ``region_stable``
        # postcondition rejected the drifted render (the ladder resolved a
        # point positionally, observed on macOS) or the ladder could not
        # resolve the unlabeled anchor at all. Both are safe -- nothing was
        # clicked past step_000 and nothing was saved.
        assert failed.postconditions_ok is False or failed.resolution is None, describe(
            report, state
        )


class TestFontFamilySubstitution:
    """Font-FAMILY substitution (glyph metrics change, sizes do not) is the
    one cosmetic axis the heal ladder can absorb: text is unchanged, so OCR
    still reads every label and heals the drifted templates. A proportional
    face passes end-to-end; a metrically extreme face (monospace, which
    widens the referral table enough to disturb the row's identity band)
    safe-halts at the pre-click identity gate. Either way: fail-safe."""

    @pytest.mark.parametrize(
        "family",
        ["Georgia, serif", '"Times New Roman", serif', '"Courier New", monospace'],
    )
    def test_font_family_substitution_never_wrong_writes(
        self, bundle, mockmed_url, _browser, run_dir, family
    ) -> None:
        report, state = replay_cosmetic(
            _browser,
            bundle.dir,
            mockmed_url,
            run_dir,
            params=dict(PARAMS),
            font_family=family,
        )
        # Platform glyph availability varies, so pin the SAFE invariant, not
        # a specific pass/halt: a run either completes on the target patient
        # or halts having saved nothing -- never a wrong write.
        assert _no_wrong_write(state), describe(report, state)
        if report.success:
            assert state["hash"] == TARGET_HASH, describe(report, state)
        else:
            assert state["banner"] is None, describe(report, state)


class TestNoWrongActionAcrossSweep:
    """The headline safety property, stated as one assertion over a
    representative cosmetic sweep: no cosmetic render drift EVER causes a
    save to the wrong patient. A single wrong-action here would be a
    headline finding (the ratio-framed safety claim would be false)."""

    def test_cosmetic_sweep_is_fail_safe(
        self, bundle, mockmed_url, _browser, run_dir
    ) -> None:
        sweep = [
            {"zoom": 0.80},
            {"zoom": 1.25},
            {"zoom": 1.75},
            {"device_scale_factor": 2},
            {"font_scale": 1.1875},
            {"font_family": "Georgia, serif"},
            {"zoom": 1.33, "device_scale_factor": 1.5},
        ]
        wrong_actions = []
        for i, kwargs in enumerate(sweep):
            report, state = replay_cosmetic(
                _browser,
                bundle.dir,
                mockmed_url,
                run_dir / f"p{i}",
                params=dict(PARAMS),
                **kwargs,
            )
            if not _no_wrong_write(state) or (
                report.success and state["hash"] != TARGET_HASH
            ):
                wrong_actions.append((kwargs, describe(report, state)))
        assert not wrong_actions, "\n\n".join(
            f"WRONG-ACTION under {k}:\n{d}" for k, d in wrong_actions
        )
