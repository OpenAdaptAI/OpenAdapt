"""Unit tests for the local-VLM identity comparator probe.

These are FAST and deterministic: they exercise the rendering, crop
composition, pixel-diff, and the VETO-ONLY answer parsing WITHOUT loading any
VLM or making any network / API call. The heavy end-to-end measurement
(``run_probe``) is exercised by the CLI and its committed outputs under
``benchmark/vlm_identity/``; it is intentionally not run here because loading
an MLX model is out of scope for the unit suite.
"""

from __future__ import annotations

import io

import pytest

from openadapt_flow.validation.vlm_identity_probe import (
    COLLAPSE_PAIRS,
    DRIFT_CONDS,
    RECORD_COND,
    SAME_VALUES,
    RenderCond,
    compose_pair,
    parse_veto,
    pixel_diff_fraction,
    render_crop,
)

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


# ---------------------------------------------------------------------------
# Veto-only parsing: only a clean confident SAME grants a pass; everything
# else (DIFFERENT, garbled loop, empty, hedge) is a veto -> "different".
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("SAME", "same"),
        ("same", "same"),
        ("  Same.", "same"),
        ("YES", "same"),
        ("DIFFERENT", "different"),
        ("different", "different"),
        ("NO", "different"),
        ("DIFFERERERERER", "different"),  # degenerate loop still vetoes
        ("", "different"),  # empty -> veto
        ("   ", "different"),
        ("not the same", "different"),  # hedge is not a clean SAME
        ("I am unsure", "different"),
    ],
)
def test_parse_veto(raw, expected):
    assert parse_veto(raw) == expected


def test_parse_veto_none_is_veto():
    assert parse_veto(None) == "different"


# ---------------------------------------------------------------------------
# Corpus invariants.
# ---------------------------------------------------------------------------


def test_collapse_pairs_are_all_distinct_strings():
    # Every "collapse" pair is a DIFFERENT identifier (distinct byte strings);
    # the whole point is that they render close but are not equal.
    for _cls, a, b, _note in COLLAPSE_PAIRS:
        assert a != b, f"{a} and {b} must be different strings"


def test_collapse_pairs_have_flagship_and_classes():
    classes = {cls for cls, *_ in COLLAPSE_PAIRS}
    assert {"digit_flanked_O0", "alpha_flanked_O0", "letter_l_one"} <= classes
    flat = {(a, b) for _c, a, b, _n in COLLAPSE_PAIRS}
    assert ("MG4408", "MG44O8") in flat  # the flagship O/0 collapse


def test_same_values_have_no_confusable_glyphs():
    # Clean same-value corpus must avoid O/0/l/1 so drift is isolated.
    for v in SAME_VALUES:
        assert not (set(v) & set("Ol")), f"{v} should avoid O/l confusables"


def test_drift_conditions_cover_theme_zoom_font():
    names = {c.name for c in DRIFT_CONDS}
    assert names == {"drift_dark_theme", "drift_zoom_120", "drift_serif_font"}
    # Dark theme flips colour; zoom raises font_px; serif changes family.
    by = {c.name: c for c in DRIFT_CONDS}
    assert by["drift_dark_theme"].theme == "dark"
    assert by["drift_zoom_120"].font_px > RECORD_COND.font_px
    assert by["drift_serif_font"].font_family != RECORD_COND.font_family


# ---------------------------------------------------------------------------
# pixel_diff_fraction: identical -> 0; disjoint -> > 0.
# ---------------------------------------------------------------------------


def _png(color, size=(40, 20)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format="PNG")
    return out.getvalue()


def test_pixel_diff_identical_is_zero():
    p = _png((255, 255, 255))
    assert pixel_diff_fraction(p, p) == 0.0


def test_pixel_diff_black_vs_white_is_one():
    assert pixel_diff_fraction(_png((0, 0, 0)), _png((255, 255, 255))) == pytest.approx(
        1.0
    )


def test_pixel_diff_partial_is_between():
    d = pixel_diff_fraction(_png((255, 255, 255)), _png((128, 128, 128)))
    assert 0.0 < d < 1.0


# ---------------------------------------------------------------------------
# compose_pair: produces a single valid PNG taller than either input.
# ---------------------------------------------------------------------------


def test_compose_pair_shape():
    a = _png((255, 255, 255), (60, 20))
    b = _png((200, 200, 200), (80, 24))
    out = compose_pair(a, b)
    img = Image.open(io.BytesIO(out))
    assert img.format == "PNG"
    # Stacked vertically with padding+gap: height exceeds the sum of inputs.
    assert img.height > 20 + 24
    assert img.width >= 80


# ---------------------------------------------------------------------------
# Rendering (needs Playwright + chromium; skipped cleanly if unavailable).
# ---------------------------------------------------------------------------


def _playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401

        return True
    except Exception:
        return False


@pytest.mark.skipif(not _playwright_available(), reason="playwright not installed")
def test_render_crop_produces_png():
    try:
        png = render_crop("MG4408", RECORD_COND)
    except Exception as exc:  # chromium binary may be missing in CI
        pytest.skip(f"playwright render unavailable: {exc}")
    img = Image.open(io.BytesIO(png))
    assert img.format == "PNG"
    assert img.width > 10 and img.height > 10


@pytest.mark.skipif(not _playwright_available(), reason="playwright not installed")
def test_render_crop_dark_theme_differs_from_light():
    try:
        light = render_crop("MG4482", RECORD_COND)
        dark = render_crop("MG4482", RenderCond("d", "Arial", 44, 2, "dark"))
    except Exception as exc:
        pytest.skip(f"playwright render unavailable: {exc}")
    # Same value, inverted theme -> pixels differ substantially (this is the
    # exact case cheap pixel-compare false-halts on and the VLM must survive).
    assert pixel_diff_fraction(light, dark) > 0.3
