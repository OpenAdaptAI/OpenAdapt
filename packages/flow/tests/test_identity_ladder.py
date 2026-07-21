"""Integrated identity ladder: pixel-compare + optional VLM-veto tiers.

Pins the promotion of the two validated probes into real ladder tiers and the
ladder's FAIL-SAFE invariants:

- the pixel tier's three-way verdict (verify / mismatch / abstain) and its
  structural inability to false-accept;
- the VLM tier's veto-only, config-gated, optional behaviour (off by default,
  can only reject, never overrides an earlier mismatch);
- the full ladder order structured -> pixel -> vlm -> ocr, fall-through, and
  0-false-accept across substrate configs.

Crop-level tests synthesize identifier crops with PIL (no browser); the
end-to-end substrate-config numbers live in the measurement harness
(benchmark/identity_ladder), exercised by ``test_harness_*`` under an
``importorskip`` guard so the default suite stays browser-free.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from openadapt_flow.ir import IdentityCheck
from openadapt_flow.runtime import identity as I


# ---------------------------------------------------------------------------
# Controlled identifier crops (no browser, no font): build the canonical
# grayscale canvas directly so the pixel metrics are deterministic. The
# REAL browser-rendered separation (threshold ~0.049, AUC 1.0) is pinned by
# the validated probe (benchmark/pixel_identity) and re-measured end to end
# in test_harness_* below; here we exercise the tier's DECISION boundaries.
# ---------------------------------------------------------------------------

_H, _W = I.PIXEL_CANON  # crops built at canonical size -> _canon is identity


def _png(arr: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", arr)
    assert ok
    return buf.tobytes()


def _blank(val: int = 255) -> np.ndarray:
    return np.full((_H, _W), val, np.uint8)


def _with_bar(cols: int, x0: int = 100, val: int = 0) -> np.ndarray:
    """A blank white canvas with one narrow full-height black bar -- a single
    LOCALIZED differing 'glyph cell'."""
    img = _blank()
    img[:, x0 : x0 + cols] = val
    return img


# ---------------------------------------------------------------------------
# pixel tier: verify / mismatch / abstain
# ---------------------------------------------------------------------------


def test_pixel_verify_is_config_gated_off_by_default_identical_crop_abstains() -> None:
    # VERIFY is CONFIG-GATED with a safe default off (PIXEL_VERIFY_ENABLED
    # False), pending a real remote-display battery. Under the default even a
    # byte-identical crop ABSTAINS (None) rather than verify -- so the default
    # install cannot false-accept. The jitter-robust ENABLED path and its
    # zero-false-accept battery are covered in test_pixel_identity_aligned.py.
    assert I.PIXEL_VERIFY_ENABLED is False
    png = _png(_with_bar(6))
    assert I.verify_pixel_identity(png, png) is None
    # ...but opting in per risk class verifies a byte-identical crop.
    assert I.verify_pixel_identity(png, png, enable_verify=True).status == "verified"


def test_pixel_mismatch_on_localized_glyph_change_same_render() -> None:
    # Same (blank) render, one narrow localized column differs: a single
    # differing glyph cell -> a genuinely different identifier -> mismatch.
    rec = _png(_blank())
    live = _png(_with_bar(5))
    check = I.verify_pixel_identity(rec, live)
    assert check is not None
    assert check.status == "mismatch"
    assert check.mode == "pixel"


def test_pixel_abstains_on_whole_crop_drift() -> None:
    # A WHOLE-crop change (a mid-gray wash over everything) -> drift -> abstain.
    rec = _png(_blank(255))
    live = _png(_blank(180))  # every pixel shifted -> global change
    assert I.verify_pixel_identity(rec, live) is None


def test_pixel_never_false_accepts_a_different_identifier() -> None:
    # A different identifier must NEVER verify, stable or drifted.
    rec = _png(_blank())
    stable_diff = _png(_with_bar(5))  # localized -> mismatch
    drift_diff = _png(_blank(120))  # whole-crop -> abstain
    for live in (stable_diff, drift_diff):
        check = I.verify_pixel_identity(rec, live)
        assert check is None or check.status != "verified"


def test_pixel_abstains_when_crop_missing() -> None:
    assert I.verify_pixel_identity(None, _png(_blank())) is None
    assert I.verify_pixel_identity(_png(_blank()), None) is None


# ---------------------------------------------------------------------------
# Blocker 2: crop-scale sensitivity — a one-glyph-different MRN at a REALISTIC
# cell crop size must MISMATCH (not dilute below an absolute threshold and
# false-accept), and the decision must be SCALE-INVARIANT.
# ---------------------------------------------------------------------------


def _deterministic_font_path() -> str | None:
    """Path to a scalable TrueType font that renders IDENTICALLY on every
    platform the suite runs on.

    matplotlib (a guaranteed dev/CI dependency -- the ``dev`` extra in
    pyproject.toml) bundles a fixed DejaVuSans; preferring it makes the
    pixel-comparison renders below byte-for-byte reproducible on macOS dev boxes
    and Linux CI alike. The earlier "macOS Arial, Linux DejaVu" split rendered
    the same identifier differently per host, which is what made the pixel
    metrics -- and the scale-invariance assertion -- host-dependent and flaky.
    Platform DejaVu/Arial paths are only a fallback; ``None`` means no real
    vector font is available (``ImageFont.load_default()`` is a tiny bitmap font
    too degenerate for these pixel comparisons) and the render tests should skip.
    """
    try:  # the bundled, version-pinned DejaVuSans: identical bytes everywhere
        import matplotlib.font_manager as fm

        path = fm.findfont("DejaVu Sans", fallback_to_default=False)
        if path and Path(path).exists():
            return path
    except Exception:
        pass
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",  # Debian/Ubuntu
        "/System/Library/Fonts/Supplemental/Arial.ttf",  # macOS
    ):
        if Path(path).exists():
            return path
    return None


def _scalable_font(size: int):
    from PIL import ImageFont

    path = _deterministic_font_path()
    if path is None:
        pytest.skip("no deterministic scalable TrueType font available")
    return ImageFont.truetype(path, size)


def _mrn_cell(text: str, *, width: int, jitter: int = 0) -> bytes:
    """A realistic MRN CELL crop: the MRN drawn small in a wide padded cell
    (PIL, so no browser). ``width`` varies the cell scale; ``jitter`` shifts
    the text a sub-pixel-equivalent amount (a cross-render artifact)."""
    from PIL import Image, ImageDraw

    img = Image.new("L", (width, 40), 255)
    draw = ImageDraw.Draw(img)
    font = _scalable_font(22)
    draw.text((8 + jitter, 8), text, fill=20, font=font)
    return _png(np.array(img))


def test_blocker2_wide_cell_different_mrn_does_not_false_accept() -> None:
    # The exact Blocker-2 shape: a WIDE cell where a one-digit-different MRN's
    # diff dilutes below the OLD absolute threshold. It must NOT verify.
    rec = _mrn_cell("AC50061", width=420)
    diff = _mrn_cell("AC58061", width=420)  # one digit different
    check = I.verify_pixel_identity(rec, diff)
    assert check is None or check.status != "verified"  # never a false-accept
    assert check is not None and check.status == "mismatch"  # affirmatively caught


def test_blocker2_mismatch_is_scale_invariant() -> None:
    # A genuinely different MRN (one DIGIT different) MISMATCHES at every
    # realistic cell width -- the Blocker-2 anti-dilution property: a real
    # single-glyph change stays a uniquely-localized spike and never dilutes
    # below the mismatch threshold as the surrounding cell widens.
    #
    # This uses a digit change (0 -> 8) -- the same genuinely-different
    # identifier the wide-cell sibling above proves -- NOT an O/0 homonym. A pure
    # O/0 confusable is deliberately OUTSIDE the pixel tier's remit: the two
    # glyphs are near-identical, so the pixel tier correctly ABSTAINS (its
    # localized-spike uniqueness heuristic cannot separate a homonym from
    # same-value anti-aliasing jitter) and defers the confusable to the
    # OCR-canonicalization tier. Asserting a pixel MISMATCH on an O/0 homonym
    # therefore asserted behaviour the tier does not guarantee, and the verdict
    # flipped with the host font -- the source of the flake.
    for width in (120, 240, 420, 840):
        rec = _mrn_cell("AC50061", width=width)
        diff = _mrn_cell("AC58061", width=width)  # one digit different
        check = I.verify_pixel_identity(rec, diff)
        assert check is not None and check.status == "mismatch", width


def test_blocker2_verify_gated_even_for_same_value() -> None:
    # A same-value re-render (with jitter) does NOT verify (gate) -- it either
    # abstains or safely mismatches, never grants a pass.
    rec = _mrn_cell("AC50061", width=420)
    same = _mrn_cell("AC50061", width=420, jitter=1)
    check = I.verify_pixel_identity(rec, same)
    assert check is None or check.status != "verified"


# ---------------------------------------------------------------------------
# VLM veto tier: optional, gated, veto-only
# ---------------------------------------------------------------------------


class _FakeVLM:
    def __init__(self, verdict: str, *, boom: bool = False) -> None:
        self.verdict = verdict
        self.boom = boom
        self.calls = 0

    def same_or_different(self, recorded_png: bytes, live_png: bytes) -> str:
        self.calls += 1
        if self.boom:
            raise RuntimeError("model unavailable")
        return self.verdict


def test_vlm_tier_off_by_default_returns_none() -> None:
    # No verifier injected -> tier abstains (optional; default install = no model).
    assert (
        I.verify_vlm_identity(b"a", b"b", verifier=None, glyph_confusable=True) is None
    )


def test_vlm_tier_only_runs_on_confusable_identifier() -> None:
    vlm = _FakeVLM("different")
    assert (
        I.verify_vlm_identity(b"a", b"b", verifier=vlm, glyph_confusable=False) is None
    )
    assert vlm.calls == 0  # not even consulted for a non-confusable id


def test_vlm_is_veto_only_same_abstains_different_vetoes() -> None:
    # TRULY veto-only (secondary fix, 8th reopening review): a "same" answer
    # must NOT grant a pass -- it ABSTAINS (returns None), leaving the
    # decision to prior/other evidence (and, absent any, HALT). Only
    # "different" produces a verdict, and it is always a mismatch (veto).
    same = I.verify_vlm_identity(
        b"a", b"b", verifier=_FakeVLM("same"), glyph_confusable=True
    )
    diff = I.verify_vlm_identity(
        b"a", b"b", verifier=_FakeVLM("different"), glyph_confusable=True
    )
    assert same is None  # cannot by itself certify identity
    assert diff.status == "mismatch" and diff.mode == "vlm"


def test_vlm_same_cannot_upgrade_unverified_target_to_verified() -> None:
    # In the pixel-abstain path the VLM is the only remaining signal; a
    # "same" answer must NOT upgrade the target -> the ladder HALTs (unreadable),
    # never a VLM-granted pass.
    out = I.run_identity_ladder(
        [
            lambda: None,  # pixel abstains (drift)
            lambda: I.verify_vlm_identity(
                b"a", b"b", verifier=_FakeVLM("same"), glyph_confusable=True
            ),
        ]
    )
    assert out.status != "verified"
    assert out.status == "unreadable"


def test_vlm_unsure_or_broken_model_vetoes_never_passes() -> None:
    # Veto-only: a garbled verdict, and an exception, both HALT.
    garbled = I.verify_vlm_identity(
        b"a", b"b", verifier=_FakeVLM("maybe?"), glyph_confusable=True
    )
    broken = I.verify_vlm_identity(
        b"a", b"b", verifier=_FakeVLM("same", boom=True), glyph_confusable=True
    )
    assert garbled.status == "mismatch"
    assert broken.status == "mismatch"


def test_identity_rests_on_confusable_identifier() -> None:
    assert I.identity_rests_on_confusable_identifier("Row MG44O8 Active") is True
    assert I.identity_rests_on_confusable_identifier("John Smith 1970-01-01") is False
    assert I.identity_rests_on_confusable_identifier(None) is False


# ---------------------------------------------------------------------------
# Full ladder order + fall-through + no-override
# ---------------------------------------------------------------------------


def test_ladder_order_structured_beats_pixel() -> None:
    struct = lambda: IdentityCheck(status="verified", mode="structured")
    pixel = lambda: IdentityCheck(status="mismatch", mode="pixel")
    out = I.run_identity_ladder([struct, pixel])
    assert out.mode == "structured"  # higher tier wins, pixel never consulted


def test_ladder_pixel_mismatch_not_overridden_by_vlm_or_ocr() -> None:
    pixel = lambda: IdentityCheck(status="mismatch", mode="pixel")
    vlm = lambda: IdentityCheck(status="verified", mode="vlm")
    ocr = lambda: IdentityCheck(status="verified", mode="context")
    out = I.run_identity_ladder([pixel, vlm, ocr])
    assert out.status == "mismatch" and out.mode == "pixel"


def test_ladder_abstain_falls_through_to_next_tier() -> None:
    pixel = lambda: None  # abstain (drift)
    vlm = lambda: IdentityCheck(status="mismatch", mode="vlm")
    out = I.run_identity_ladder([pixel, vlm])
    assert out.mode == "vlm"


def test_ladder_all_abstain_is_unreadable_halt() -> None:
    out = I.run_identity_ladder([lambda: None, lambda: None, lambda: None])
    assert out.status == "unreadable"  # nothing verified -> caller HALTS


# ---------------------------------------------------------------------------
# 0-false-accept across configs, at the tier level (no browser)
# ---------------------------------------------------------------------------


def test_zero_false_accept_wrong_identifier_across_configs() -> None:
    """A wrong identifier is never VERIFIED whether the top tier is structured,
    pixel, or the VLM veto -- the safety invariant, browser-free."""
    rec_str, live_str = "MG4408", "MG44O8"
    rec_png = _png(_blank())
    live_png = _png(_with_bar(5))  # a different identifier (localized glyph)

    # structured config
    s = I.run_identity_ladder([lambda: I.verify_structured_identity(rec_str, live_str)])
    # pixel config
    p = I.run_identity_ladder([lambda: I.verify_pixel_identity(rec_png, live_png)])
    # pixel+vlm config (drift -> pixel abstains -> vlm vetoes)
    v = I.run_identity_ladder(
        [
            lambda: None,
            lambda: I.verify_vlm_identity(
                rec_png, live_png, verifier=_FakeVLM("different"), glyph_confusable=True
            ),
        ]
    )
    for out in (s, p, v):
        assert out.status != "verified"  # wrong patient NEVER verifies


# ---------------------------------------------------------------------------
# Replayer wiring: pixel/VLM tiers reached via a bundle identifier_crop
# ---------------------------------------------------------------------------

from openadapt_flow.ir import (  # noqa: E402
    ActionKind,
    Anchor,
    Resolution,
    Step,
    Workflow,
)
from openadapt_flow.runtime.replayer import Replayer  # noqa: E402

_VP = (_W, _H)


class _Backend:
    viewport = _VP

    def screenshot(self):
        return _png(_blank())

    def click(self, x, y, *, double=False): ...

    def wait_settled(self, **kw):
        return self.screenshot()


def _bundle_with_crop(tmp_path, crop_arr) -> tuple:
    (tmp_path / "templates").mkdir(parents=True, exist_ok=True)
    (tmp_path / "templates" / "id.png").write_bytes(_png(crop_arr))
    (tmp_path / "templates" / "t.png").write_bytes(_png(_blank()))
    step = Step(
        id="s1",
        intent="click row",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template="templates/t.png",
            region=(0, 0, _W, _H),
            click_point=(0, 0),
            identifier_crop="templates/id.png",
            identifier_region=(0, 0, _W, _H),
        ),
    )
    res = Resolution(rung="template", point=(0, 0), confidence=0.9, elapsed_ms=1.0)
    return step, res


def test_replayer_pixel_tier_matching_crop_abstains_verify_gated(tmp_path) -> None:
    # Blocker 2: the pixel VERIFY path is HARD-GATED, so even a matching crop
    # ABSTAINS rather than verify; with no context_text the OCR tier also
    # abstains -> unreadable HALT. The pixel tier can never grant a pass.
    step, res = _bundle_with_crop(tmp_path, _blank())
    rp = Replayer(_Backend(), vision=object(), poll_interval_s=0.01)
    check = rp._verify_identity(
        step, res, _png(_blank()), {}, Workflow(name="wf"), tmp_path
    )
    assert check.status != "verified"
    assert check.status == "unreadable"


def test_replayer_pixel_tier_mismatch_halts_wrong_identifier(tmp_path) -> None:
    # Recorded crop has a localized bar the live blank frame lacks -> a
    # different identifier on a matching render -> pixel MISMATCH -> halt.
    step, res = _bundle_with_crop(tmp_path, _with_bar(5))
    rp = Replayer(_Backend(), vision=object(), poll_interval_s=0.01)
    check = rp._verify_identity(
        step, res, _png(_blank()), {}, Workflow(name="wf"), tmp_path
    )
    assert check.status == "mismatch" and check.mode == "pixel"


def test_replayer_vlm_tier_off_by_default_no_crop_falls_through(tmp_path) -> None:
    # No identifier_crop and no VLM: pixel+vlm abstain; with no context_text
    # the OCR tier also abstains -> unreadable HALT (default install, no model).
    step = Step(
        id="s1",
        intent="click",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template="templates/t.png",
            region=(0, 0, _W, _H),
            click_point=(0, 0),
            structured_identity="MG44O8 X",
        ),
    )
    res = Resolution(rung="template", point=(0, 0), confidence=0.9, elapsed_ms=1.0)

    class _NoStruct(_Backend):
        pass  # no structured_text_at

    rp = Replayer(_NoStruct(), vision=object(), poll_interval_s=0.01)
    check = rp._verify_identity(
        step, res, _png(_blank()), {}, Workflow(name="wf"), tmp_path
    )
    assert check.status == "unreadable"  # nothing verified -> HALT, no model


# ---------------------------------------------------------------------------
# Integrated harness (browser) -- guarded
# ---------------------------------------------------------------------------


@pytest.mark.timeout(900)  # heavy browser+OCR integration; ~350s local, slower CI
def test_harness_zero_false_accept_all_configs(tmp_path) -> None:
    pytest.importorskip("playwright")
    from openadapt_flow.validation import identity_ladder as H

    summary = H.run(tmp_path)
    # THE safety invariant, measured on the REAL Replayer._verify_identity
    # production tier stack (the OCR tier the replayer always appends is in the
    # stack for every config): 0 false-accept everywhere, incl. the homonym.
    assert summary["safety_invariant_false_accept_zero_all_configs"] is True
    cfgs = summary["configs"]
    assert all(c["false_accept"] == 0 for c in cfgs.values())
    # the structured-text substrate still verifies the correct patient (O/0
    # distinct in the DOM): no over-halt there.
    assert cfgs["structured"]["over_halt"] == 0
    # the OCR-only-confusable config the flawed harness never measured now
    # shows HIGH over-halt (OCR alone cannot verify a collapsible MRN).
    assert cfgs["ocr_only_confusable"]["over_halt_rate"] == 1.0
