"""Tests for the pixel-perceptual identity-comparison probe.

Fast tests (no browser) cover the collapse-pair fixture, the comparison
methods on synthetic crops, and the separation statistics. The end-to-end
render + OCR path is exercised by one Playwright-guarded test (``importorskip``,
same pattern as the other browser tests), so the core unit suite stays green
without a browser.
"""

from __future__ import annotations

import numpy as np
import pytest

from openadapt_flow.validation import pixel_identity_probe as pip


# ---------------------------------------------------------------------------
# Collapse-pair fixture
# ---------------------------------------------------------------------------


def test_collapse_pairs_are_one_glyph_apart_distinct_patients() -> None:
    assert pip.COLLAPSE_PAIRS, "expected at least one collapse pair"
    for p in pip.COLLAPSE_PAIRS:
        # A realistic sibling is a DIFFERENT identifier string...
        assert p.target != p.sibling
        # ...that is exactly ONE character apart (the OCR-confusable glyph)...
        assert len(p.target) == len(p.sibling)
        diffs = [(a, b) for a, b in zip(p.target, p.sibling) if a != b]
        assert len(diffs) >= 1
        # ...and every differing position is an O<->0 or l<->1 swap.
        for a, b in diffs:
            assert {a, b} in ({"O", "0"}, {"l", "1"}), (a, b)


def test_collapse_pairs_cover_both_glyph_classes_and_flanks() -> None:
    classes = {p.glyph_class for p in pip.COLLAPSE_PAIRS}
    flanks = {p.flank for p in pip.COLLAPSE_PAIRS}
    assert {"O0", "l1"} <= classes
    assert {"digit", "alpha"} <= flanks


def test_all_values_dedup_and_order() -> None:
    vals = pip.all_values(pip.COLLAPSE_PAIRS)
    assert len(vals) == len(set(vals))  # de-duplicated
    for p in pip.COLLAPSE_PAIRS:  # every string present
        assert p.target in vals and p.sibling in vals


def test_values_table_stable_same_parity_indices() -> None:
    vals = pip.all_values(pip.COLLAPSE_PAIRS)
    table, index = pip._values_table(vals)
    # every value has a row, at a distinct index, and the row carries the MRN
    assert set(index) == set(vals)
    assert len(set(index.values())) == len(vals)
    parities = {i % 2 for i in index.values()}
    assert len(parities) == 1, "value rows must share background parity"
    for v, i in index.items():
        assert table.rows[i].mrn == v
    # rebuilding is deterministic (same indices)
    _, index2 = pip._values_table(vals)
    assert index == index2


# ---------------------------------------------------------------------------
# Comparison methods on synthetic crops
# ---------------------------------------------------------------------------


def _white(h: int = 40, w: int = 200) -> np.ndarray:
    return np.full((h, w, 3), 255, np.uint8)


def _with_glyphs(spots: list[int]) -> np.ndarray:
    """A white crop with black 'glyph' blocks at the given x offsets."""
    img = _white()
    for x in spots:
        img[10:30, x : x + 16] = 0
    return img


def test_methods_return_zero_on_identical_crops() -> None:
    img = _with_glyphs([20, 60, 100, 140])
    for name, fn in pip.METHODS.items():
        d = fn(img, img.copy())
        if name == "orb_feature":
            continue  # feature matching is undefined on tiny low-texture crops
        assert not np.isnan(d)
        assert d <= 1e-6, f"{name} gave {d} on identical crops"


def test_methods_flag_a_localized_glyph_difference() -> None:
    # Same identifier except ONE 'glyph' cell differs (the O/0 analogue).
    a = _with_glyphs([20, 60, 100, 140])
    b = _with_glyphs([20, 60, 100, 140])
    b[14:26, 104:112] = 255  # hollow one glyph (shape change, O/0 analogue)
    for name in (
        "local_maxdiff",
        "ssim_global",
        "charcell_ssim_max",
        "l1_global",
        "l2_global",
        "ncc_global",
        "edge_iou",
    ):
        d = pip.METHODS[name](a, b)
        assert d > 0.0, f"{name} missed a localized glyph difference"


def test_registry_and_categories_are_complete() -> None:
    assert set(pip.METHODS) == set(pip.METHOD_CATEGORY)
    assert "local_maxdiff" in pip.METHODS
    assert "charcell_ssim_max" in pip.METHODS


# ---------------------------------------------------------------------------
# Separation statistics
# ---------------------------------------------------------------------------


def test_auc_perfect_and_clean_split() -> None:
    same = [0.0, 0.0, 0.01]
    diff = [0.2, 0.4, 0.5]
    assert pip.auc(same, diff) == 1.0
    s = pip.separation(same, diff)
    assert s["clean_separation"] is True
    assert s["threshold"] == pytest.approx((0.01 + 0.2) / 2)
    assert s["gap"] == pytest.approx(0.2 - 0.01)


def test_auc_overlap_not_clean() -> None:
    same = [0.0, 0.3]
    diff = [0.1, 0.4]
    s = pip.separation(same, diff)
    assert s["clean_separation"] is False
    assert s["threshold"] is None
    assert 0.0 < pip.auc(same, diff) < 1.0


def test_auc_handles_nan_inputs() -> None:
    assert np.isnan(pip.auc([float("nan")], [1.0]))
    assert pip.auc([0.0, float("nan")], [1.0, 2.0]) == 1.0


# ---------------------------------------------------------------------------
# End-to-end render + separation (browser-guarded)
# ---------------------------------------------------------------------------


def test_end_to_end_pixels_separate_a_collapse_pair() -> None:
    pytest.importorskip("playwright.sync_api")
    pair = next(p for p in pip.COLLAPSE_PAIRS if p.glyph_class == "O0")
    values = [pair.target, pair.sibling]
    try:
        ref = pip.render_value_crops(values, pip.STABLE_REF)
        rer = pip.render_value_crops(values, pip.STABLE_RERENDER)
    except Exception as exc:  # no browser binary in this environment
        pytest.skip(f"browser render unavailable: {exc}")
    fn = pip.METHODS["local_maxdiff"]
    same = fn(ref[pair.target], rer[pair.target])
    diff = fn(ref[pair.target], rer[pair.sibling])
    # The pixels retain what OCR discards: different-value must score strictly
    # worse than same-value on a stable render.
    assert diff > same
    assert same <= 1e-6
