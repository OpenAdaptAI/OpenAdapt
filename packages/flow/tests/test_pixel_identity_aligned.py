"""Jitter-robust pixel-identity battery: the VERIFY-safety invariants.

Exercises the positive MATCH path of the pixel identity tier
(:func:`openadapt_flow.runtime.identity.verify_pixel_identity`) that
``PIXEL_VERIFY_ENABLED`` gates, proving it is safe to certify a correct record
without ever false-accepting a wrong one. Self-contained (``cv2``+``numpy``, no
browser, no system fonts) via
:mod:`openadapt_flow.validation.pixel_identity_aligned`.

The HARD requirement is zero false-accept (a different record must NEVER MATCH);
a false-mismatch (over-halt on the correct record) is a safe fallback and is
merely bounded, not forbidden.
"""

from __future__ import annotations

import cv2
import numpy as np

from openadapt_flow.runtime import identity as I
from openadapt_flow.validation import pixel_identity_aligned as B


def _png(arr: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", arr)
    assert ok
    return bytes(buf.tobytes())


# ---------------------------------------------------------------------------
# The safety invariant: zero false-accept across the whole battery
# ---------------------------------------------------------------------------


def test_zero_false_accept_across_jitter_battery() -> None:
    """No different record (glyph-collapse sibling OR wrong MRN) ever MATCHES,
    under any jitter/compression/scale/theme condition, even with VERIFY forced
    on. This is THE hard requirement."""
    res = B.run_battery(enable_verify=True)
    s = res["summary"]
    assert s["n_diff"] >= 400  # a substantial battery
    assert s["false_accept"] == 0, [
        r for r in res["rows"] if r[0].startswith("diff") and r[3] == "match"
    ]
    assert s["n_diff_wrong_matched"] == 0


def test_verify_gate_sits_in_a_real_gap_not_a_knife_edge() -> None:
    """The whole-crop match statistic cleanly separates same-record matching
    renders from every different record, and the VERIFY gate sits inside that
    gap with margin (so zero-false-accept is robust, not incidental)."""
    d = B.distance_stats()
    # every different record is above the gate by a healthy margin
    assert d["diff_min_max_window"] > d["gate"] + 0.02
    # same-record matching renders separate from different records
    assert d["same_matching_max_window_max"] < d["diff_min_max_window"]
    assert d["gap"] > 0.0


# ---------------------------------------------------------------------------
# Utility: the correct record matches, the wrong one halts (on matching renders)
# ---------------------------------------------------------------------------


def test_same_record_matches_on_matching_renders() -> None:
    res = B.run_battery(enable_verify=True)
    # a useful fraction of correct records certify under sub-pixel jitter / mild
    # compression (the rest safely abstain -> OCR/HALT); over-halt stays low.
    assert res["summary"]["same_match_rate_matching_render"] >= 0.5
    assert res["summary"]["same_mismatch_rate"] <= 0.05


def test_glyph_collapse_sibling_halts_on_matching_render() -> None:
    """The OCR-blind wrong patient (one O/0 or l/1 apart) is caught as a pixel
    MISMATCH on a matching render more often than not, and NEVER matches."""
    res = B.run_battery(enable_verify=True)
    assert res["summary"]["diff_collapse_mismatch_rate_matching_render"] >= 0.4


# ---------------------------------------------------------------------------
# Adversarial cases from vision_hardening_2026_07_20 (P1-P4) + perfect render
# ---------------------------------------------------------------------------


def test_perfect_render_collapse_pair_never_matches() -> None:
    """P1/the OCR-blind case: a glyph-collapse sibling on a byte-for-byte
    matching render must MISMATCH (or abstain), NEVER verify."""
    for tgt, sib in B.COLLAPSE_PAIRS:
        rec = B._to_png(B.render_mrn(tgt))
        live = B._to_png(B.render_mrn(sib))
        d = B.decide(rec, live, enable_verify=True)
        assert d != "match", (tgt, sib)


def test_wrong_patient_under_dark_theme_never_matches() -> None:
    """P2: a different MRN under theme inversion must NOT verify (the tier
    abstains under whole-crop drift and defers to OCR/HALT)."""
    for tgt, sib in B.WRONG_PAIRS:
        rec = B._to_png(B.render_mrn(tgt))
        live = B._to_png(255 - B.render_mrn(sib))
        assert B.decide(rec, live, enable_verify=True) != "match"


def test_wrong_patient_under_scale_drift_never_matches() -> None:
    """P3-analogue: a glyph-collapse sibling under 150% DPI must NOT verify."""
    for tgt, sib in B.COLLAPSE_PAIRS:
        rec = B._to_png(B.render_mrn(tgt))
        live = B._to_png(B._scale(B.render_mrn(sib), 1.5))
        assert B.decide(rec, live, enable_verify=True) != "match"


def test_same_value_under_jpeg_jitter_does_not_over_halt() -> None:
    """P4: a same-value re-render under sub-pixel jitter + JPEG must NOT be a
    MISMATCH (no false wrong-patient halt); MATCH or ABSTAIN are both fine."""
    for tgt, _sib in B.COLLAPSE_PAIRS:
        rec = B.render_mrn(tgt)
        live = B._jpeg(B._subpixel_shift(rec, 0.8, 0.6), 18)
        assert (
            B.decide(B._to_png(rec), B._to_png(live), enable_verify=True) != "mismatch"
        )


# ---------------------------------------------------------------------------
# Config gate + PHI hygiene
# ---------------------------------------------------------------------------


def test_verify_is_off_by_default_and_never_matches() -> None:
    """With the module default (VERIFY off), the tier never certifies a match —
    identical crop included — so the default install cannot false-accept."""
    assert I.PIXEL_VERIFY_ENABLED is False
    res = B.run_battery(enable_verify=False)  # default-off behavior
    assert all(r[3] != "match" for r in res["rows"])
    # a byte-identical crop abstains under the default, verifies only when opted in
    rec = B._to_png(B.render_mrn("AC50061"))
    assert I.verify_pixel_identity(rec, rec) is None
    assert I.verify_pixel_identity(rec, rec, enable_verify=True).status == "verified"


def test_identity_verdict_carries_no_phi() -> None:
    """MISMATCH/VERIFY ``observed`` text must carry only distances, never the
    crop pixels or any decoded identifier string (an identity verdict is
    logged/reported, so it must not leak PHI)."""
    rec = B._to_png(B.render_mrn("MG4408"))
    diff = B._to_png(B.render_mrn("MG44O8"))  # one O/0 glyph apart
    mismatch = I.verify_pixel_identity(rec, diff)
    assert mismatch is not None and mismatch.status == "mismatch"
    verify = I.verify_pixel_identity(rec, rec, enable_verify=True)
    assert verify is not None and verify.status == "verified"
    for check in (mismatch, verify):
        blob = f"{check.observed} {check.expected}"
        for mrn in ("MG4408", "MG44O8"):
            assert mrn not in blob and mrn.lower() not in blob.lower()


def test_alignment_shift_cap_rejects_implausible_registration() -> None:
    """A large translation between crops must NOT be 'aligned away' (which could
    slide one identifier onto another) — the shift cap disqualifies VERIFY."""
    a = np.full((48, 240), 255, np.uint8)
    a[:, 20:40] = 0  # a bar on the left
    b = np.full((48, 240), 255, np.uint8)
    b[:, 180:200] = 0  # the bar far to the right (implausible jitter)
    ga = I._pixel_canon_aspect(_png(a))
    gb = I._pixel_canon_aspect(_png(b))
    _, ok = I._align_translation(ga, gb)
    assert ok is False
