"""End-to-end: the structural (DOM) ACTION rung as the product DEFAULT.

The record -> compile -> replay characterization suites in this package run
with ``use_structural=False`` so they keep pinning the VISUAL fallback floor
(the pixel-only / RDP-Citrix substrate path). This module pins the DEFAULT
product behavior on a structure-bearing backend (MockMed is a real DOM app):
the runtime resolves each recorded target as a DOM element and acts on it
deterministically, with the identity and risk gates unchanged.

The resolution-level availability win under render drift (structural 21/21 vs
visual 6/21) is measured in ``openadapt_flow/validation/structural_action.py``
and asserted in ``tests/test_structural_rung.py``; here we prove the same rung
drives a full record->compile->replay on MockMed and preserves the headline
no-wrong-write safety property under cosmetic drift.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .conftest import PARAMS
from .test_record_compile_replay import N_ANCHORED, anchored_results, rung_of
from .validation_utils import describe, replay_cosmetic

pytestmark = pytest.mark.timeout(600)

TARGET_HASH = "#patient/p1"


def test_default_happy_path_resolves_every_step_structurally(
    bundle, mockmed_url, replay
) -> None:
    """DEFAULT product path: every anchored step resolves on the deterministic
    ``structural`` rung (DOM element), zero heals, zero model calls, saved to
    the target patient. The strongest possible resolution -- an exact element,
    not a pixel match."""
    report, _ = replay(bundle.dir, mockmed_url, use_structural=True)
    assert report.success, describe(report, {})
    assert report.heal_count == 0
    assert report.model_calls == 0
    # The whole anchored path came from the structural rung.
    assert report.rung_counts == {"structural": N_ANCHORED}, report.rung_counts
    for result in anchored_results(report):
        assert result.resolution is not None
        assert result.resolution.rung == "structural"
        # Identity gate still ran in front of every click: no click was
        # admitted on a mismatch (a mismatch aborts the run).
        if result.identity is not None:
            assert result.identity.status != "mismatch"


def test_structural_is_actually_engaged_vs_visual_floor(
    bundle, mockmed_url, replay
) -> None:
    """Direct contrast on the SAME bundle: the visual floor resolves the save
    step via ``template``; the default structural path resolves it via
    ``structural``. Proves the rung is genuinely preferred, not incidental."""
    floor, _ = replay(bundle.dir, mockmed_url, use_structural=False)
    structural, _ = replay(bundle.dir, mockmed_url, use_structural=True)
    assert floor.success and structural.success
    save_floor = rung_of(floor, "step_010")
    save_structural = rung_of(structural, "step_010")
    assert save_floor == "template"
    assert save_structural == "structural"


def test_structural_on_never_wrong_writes_under_cosmetic_drift(
    bundle, mockmed_url, _browser, tmp_path
) -> None:
    """Headline safety property holds with the structural rung ENGAGED: across
    a cosmetic drift sweep, the run either completes on the TARGET patient or
    safe-halts having saved nothing -- never a save to the wrong patient.
    Structural resolution makes identity STRONGER (an exact element); it never
    licenses a wrong action."""
    sweep = [
        {"zoom": 1.25},
        {"font_scale": 1.375},
        {"font_family": "Georgia, serif"},
        {"device_scale_factor": 2},
    ]
    for i, kwargs in enumerate(sweep):
        report, state = replay_cosmetic(
            _browser,
            bundle.dir,
            mockmed_url,
            tmp_path / f"p{i}",
            params=dict(PARAMS),
            use_structural=True,
            **kwargs,
        )
        wrong_write = state["banner"] is not None and state["hash"] != TARGET_HASH
        assert not wrong_write, describe(report, state)
        if report.success:
            assert state["hash"] == TARGET_HASH, describe(report, state)
        else:
            assert state["banner"] is None, describe(report, state)
