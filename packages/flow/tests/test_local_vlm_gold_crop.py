"""Tests for Global-to-Local (GOLD) ROI cropping and VetoOnlyVLMGuard.

Proves:
1. Zero behavior regression when ``roi`` is omitted or frames fit ``<= 1024px``.
2. Improved pixel-fidelity preservation on ``> 1024px`` synthetic frames within
   the ``<= 1024px`` model ceiling, plus deterministic coordinate round-trips.
3. Strict veto-only semantics: ``VETO`` and ``ABSTAIN`` always halt; ``PASS``
   never overrides a failed deterministic check.
"""

from __future__ import annotations

import io
from typing import Optional

import pytest
from PIL import Image, ImageDraw

from openadapt_flow.runtime.identity import IdentityVLM
from openadapt_flow.runtime.identity_vlm import (
    VetoDecision,
    VetoOnlyVLMGuard,
    VetoVerdict,
)
from openadapt_flow.runtime.remote_vlm import (
    _MAX_MODEL_IMAGE_DIM,
    IdentityVerdict,
    RemoteGrounder,
    RemoteStateVerifier,
    _downscale_for_model,
    gold_crop_for_model,
)


def _dims(png: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(png)).size


def _make_synthetic_banner_frame(
    w: int = 2560,
    h: int = 1440,
    roi: tuple[int, int, int, int] = (900, 80, 320, 64),
) -> bytes:
    """Create a >1024px frame with crisp alternating 2px vertical bars in ``roi``.

    Uniformly downscaling 2560 -> 1024 (0.4x) blurs 2px stripes into grey,
    whereas a native-resolution GOLD ROI crop preserves full black/white contrast.
    """
    img = Image.new("RGB", (w, h), (235, 235, 235))
    draw = ImageDraw.Draw(img)
    rx, ry, rw, rh = roi
    # Distinct red border around the alert banner region
    draw.rectangle(
        [rx, ry, rx + rw - 1, ry + rh - 1], fill=(255, 255, 255), outline=(220, 30, 30)
    )
    # High-frequency 2px vertical bars inside the banner (proxy for small crisp text)
    for x in range(rx + 8, rx + rw - 8, 4):
        draw.rectangle([x, ry + 12, x + 1, ry + rh - 12], fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class _StubClient:
    def __init__(self, *, ground_responses=None, state=None):
        self._ground_responses = list(ground_responses or [])
        self._state = state
        self.ground_calls: list[tuple[tuple[int, int], str]] = []
        self.state_calls: list[tuple[tuple[int, int], str]] = []
        self.last_state_png: Optional[bytes] = None

    def ground(self, png, intent, ocr_text=None):
        self.ground_calls.append((_dims(png), intent))
        if self._ground_responses:
            return self._ground_responses.pop(0)
        return None

    def verify_state(self, png, expected):
        self.state_calls.append((_dims(png), expected))
        self.last_state_png = png
        return self._state


# --------------------------------------------------------------------------
# 1. Zero behavior regression
# --------------------------------------------------------------------------


def test_gold_crop_no_roi_matches_downscale_exactly():
    frame = _make_synthetic_banner_frame(2560, 1440)
    expected_png, expected_scale = _downscale_for_model(frame)
    res = gold_crop_for_model(frame, roi=None)
    assert res.used_gold is False
    assert res.global_png == expected_png
    assert res.local_png == expected_png
    assert res.composite_png == expected_png
    assert res.global_scale == expected_scale
    assert res.local_scale == expected_scale
    assert res.local_origin == (0, 0)


def test_gold_crop_small_frame_is_untouched():
    frame = _make_synthetic_banner_frame(800, 600, roi=(100, 50, 200, 60))
    res = gold_crop_for_model(frame, roi=(100, 50, 200, 60))
    assert res.used_gold is False
    assert res.global_png == frame
    assert res.composite_png == frame
    assert res.global_scale == 1.0
    assert res.local_scale == 1.0


@pytest.mark.parametrize(
    "bad_roi",
    [
        (0, 0, 0, 50),
        (0, 0, 50, -10),
        (5000, 5000, 100, 100),
    ],
)
def test_gold_crop_invalid_roi_falls_back_cleanly(bad_roi):
    frame = _make_synthetic_banner_frame(2048, 1200)
    res = gold_crop_for_model(frame, roi=bad_roi)
    assert res.used_gold is False
    assert max(_dims(res.composite_png)) == _MAX_MODEL_IMAGE_DIM


def test_gold_crop_malformed_bytes_fail_safe():
    res = gold_crop_for_model(b"not-a-png", roi=(10, 10, 100, 50))
    assert res.used_gold is False
    assert res.composite_png == b"not-a-png"
    assert res.global_scale == 1.0


# --------------------------------------------------------------------------
# 2. Improved crop preservation on >1024px frames within the 1024px budget
# --------------------------------------------------------------------------


def test_gold_crop_preserves_native_resolution_and_high_frequency_detail():
    roi = (900, 80, 320, 64)
    frame = _make_synthetic_banner_frame(2560, 1440, roi=roi)
    res = gold_crop_for_model(frame, roi=roi)

    assert res.used_gold is True
    assert res.global_scale == pytest.approx(1024 / 2560)
    assert res.local_scale == 1.0
    assert res.local_scale > res.global_scale

    # Every output stays strictly inside the 1024px model ceiling on both axes.
    assert max(_dims(res.global_png)) <= _MAX_MODEL_IMAGE_DIM
    assert max(_dims(res.local_png)) <= _MAX_MODEL_IMAGE_DIM
    assert max(_dims(res.composite_png)) <= _MAX_MODEL_IMAGE_DIM

    # Verify high-frequency stripe contrast is preserved in local_png (black=0, white=255)
    # whereas uniform global downscaling smears the stripes.
    local_im = Image.open(io.BytesIO(res.local_png)).convert("L")
    local_extrema = local_im.getextrema()
    assert local_extrema == (0, 255)

    global_im = Image.open(io.BytesIO(res.global_png)).convert("L")
    rx, ry, rw, rh = roi
    gs = res.global_scale
    global_banner = global_im.crop(
        (
            int(rx * gs) + 4,
            int(ry * gs) + 4,
            int((rx + rw) * gs) - 4,
            int((ry + rh) * gs) - 4,
        )
    )
    g_lo, g_hi = global_banner.getextrema()
    assert (g_hi - g_lo) < 255  # downscaled banner lost full black-white contrast


def test_gold_crop_oversized_roi_stays_within_budget_and_beats_global():
    frame = _make_synthetic_banner_frame(3200, 2400, roi=(400, 300, 1400, 900))
    res = gold_crop_for_model(frame, roi=(400, 300, 1400, 900))
    assert res.used_gold is True
    assert max(_dims(res.local_png)) == _MAX_MODEL_IMAGE_DIM
    assert max(_dims(res.composite_png)) <= _MAX_MODEL_IMAGE_DIM
    assert res.local_scale > res.global_scale


def test_remote_grounder_gold_crop_maps_local_point_to_original_frame():
    roi = (900, 80, 320, 64)
    frame = _make_synthetic_banner_frame(2560, 1440, roi=roi)
    expected = gold_crop_for_model(frame, roi=roi)
    ox, oy = expected.local_origin

    stub = _StubClient(ground_responses=[{"point": [100, 40], "confidence": 0.92}])
    grounder = RemoteGrounder(stub)
    match = grounder.locate(frame, "Dismiss alert", roi=roi)

    assert len(stub.ground_calls) == 1
    assert stub.ground_calls[0][0] == _dims(expected.local_png)
    assert match is not None
    assert match.point == (ox + 100, oy + 40)
    assert match.confidence == pytest.approx(0.92)


def test_remote_grounder_gold_crop_falls_back_to_global_when_local_misses():
    roi = (900, 80, 320, 64)
    frame = _make_synthetic_banner_frame(2560, 1440, roi=roi)

    # Local ROI returns null; global pass finds target at (500, 200) in 1024x576 frame.
    stub = _StubClient(
        ground_responses=[
            {"point": None},
            {"point": [500, 200], "confidence": 0.85},
        ]
    )
    grounder = RemoteGrounder(stub)
    match = grounder.locate(frame, "Submit button", roi=roi)

    assert len(stub.ground_calls) == 2
    assert stub.ground_calls[1][0] == (1024, 576)
    assert match is not None
    assert match.point == (round(500 / (1024 / 2560)), round(200 / (1024 / 2560)))


def test_remote_state_verifier_uses_gold_composite_within_budget():
    roi = (900, 80, 320, 64)
    frame = _make_synthetic_banner_frame(2560, 1440, roi=roi)
    stub = _StubClient(state={"holds": "yes"})
    verifier = RemoteStateVerifier(stub)

    assert verifier.verify(frame, "error banner visible", roi=roi) == "yes"
    assert len(stub.state_calls) == 1
    sent_w, sent_h = stub.state_calls[0][0]
    assert max(sent_w, sent_h) <= _MAX_MODEL_IMAGE_DIM
    # Composite is taller than plain 1024x576 global downscale because it appends the ROI crop.
    assert sent_h > 576


# --------------------------------------------------------------------------
# 3. Unified Veto-Only Local VLM Guard semantics
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("det_passed", "verdict", "expected"),
    [
        (True, VetoVerdict.PASS, True),
        (True, VetoVerdict.VETO, False),
        (True, VetoVerdict.ABSTAIN, False),
        # PASS must NEVER override a failed deterministic check.
        (False, VetoVerdict.PASS, False),
        (False, VetoVerdict.VETO, False),
        (False, VetoVerdict.ABSTAIN, False),
    ],
)
def test_veto_decision_strict_veto_only_truth_table(det_passed, verdict, expected):
    decision = VetoDecision(verdict=verdict)
    assert decision.should_halt is (verdict is not VetoVerdict.PASS)
    assert decision.apply_to_deterministic(det_passed) is expected


class _FakeRemoteIdentity:
    def __init__(self, verdict: IdentityVerdict):
        self.verdict = verdict
        self.seen_dims: list[tuple[tuple[int, int], tuple[int, int]]] = []

    def compare(self, crop_a: bytes, crop_b: bytes) -> IdentityVerdict:
        self.seen_dims.append((_dims(crop_a), _dims(crop_b)))
        return self.verdict


class _FakeLocalIdentity:
    def __init__(self, answer: str):
        self.answer = answer

    def same_or_different(self, recorded_png: bytes, live_png: bytes) -> str:
        return self.answer


class _ExplodingBackend:
    def compare(self, crop_a: bytes, crop_b: bytes):
        raise RuntimeError("backend crashed")

    def verify(self, screenshot: bytes, expected_state: str):
        raise RuntimeError("backend crashed")


def test_guard_satisfies_identity_vlm_protocol():
    guard = VetoOnlyVLMGuard(identity_vlm=_FakeLocalIdentity("same"))
    assert isinstance(guard, IdentityVLM)


def test_guard_identity_uses_gold_crop_and_maps_verdicts():
    roi = (900, 80, 320, 64)
    frame = _make_synthetic_banner_frame(2560, 1440, roi=roi)

    remote_ok = _FakeRemoteIdentity(IdentityVerdict.VERIFY)
    guard_ok = VetoOnlyVLMGuard(identity_vlm=remote_ok)
    dec_ok = guard_ok.check_identity(frame, frame, roi=roi)
    assert dec_ok.verdict is VetoVerdict.PASS
    assert dec_ok.should_halt is False
    assert dec_ok.used_gold_crop is True
    assert max(remote_ok.seen_dims[0][0]) <= _MAX_MODEL_IMAGE_DIM
    assert remote_ok.seen_dims[0][0][0] < 1024  # cropped ROI, not full 1024px frame
    assert guard_ok.same_or_different(frame, frame, roi=roi) == "same"

    guard_mismatch = VetoOnlyVLMGuard(
        identity_vlm=_FakeRemoteIdentity(IdentityVerdict.MISMATCH)
    )
    dec_veto = guard_mismatch.check_identity(frame, frame, roi=roi)
    assert dec_veto.verdict is VetoVerdict.VETO
    assert dec_veto.should_halt is True
    assert guard_mismatch.same_or_different(frame, frame, roi=roi) == "different"

    guard_abstain = VetoOnlyVLMGuard(
        identity_vlm=_FakeRemoteIdentity(IdentityVerdict.ABSTAIN)
    )
    dec_abs = guard_abstain.check_identity(frame, frame, roi=roi)
    assert dec_abs.verdict is VetoVerdict.ABSTAIN
    assert dec_abs.should_halt is True
    assert guard_abstain.same_or_different(frame, frame, roi=roi) == "different"


def test_guard_local_identity_and_exceptions_fail_safe_to_halt():
    frame = _make_synthetic_banner_frame(800, 600)

    assert (
        VetoOnlyVLMGuard(identity_vlm=_FakeLocalIdentity("same"))
        .check_identity(frame, frame)
        .verdict
        is VetoVerdict.PASS
    )
    assert (
        VetoOnlyVLMGuard(identity_vlm=_FakeLocalIdentity("different"))
        .check_identity(frame, frame)
        .verdict
        is VetoVerdict.VETO
    )
    assert (
        VetoOnlyVLMGuard(identity_vlm=_FakeLocalIdentity("maybe"))
        .check_identity(frame, frame)
        .verdict
        is VetoVerdict.ABSTAIN
    )

    exploding = VetoOnlyVLMGuard(
        identity_vlm=_ExplodingBackend(), state_verifier=_ExplodingBackend()
    )
    id_dec = exploding.check_identity(frame, frame)
    st_dec = exploding.check_state(frame, "saved")
    assert id_dec.verdict is VetoVerdict.ABSTAIN and id_dec.should_halt is True
    assert st_dec.verdict is VetoVerdict.ABSTAIN and st_dec.should_halt is True

    empty = VetoOnlyVLMGuard()
    assert empty.check_identity(frame, frame).verdict is VetoVerdict.ABSTAIN
    assert empty.check_state(frame, "saved").verdict is VetoVerdict.ABSTAIN


def test_guard_state_check_uses_gold_composite_and_veto_only_semantics():
    roi = (900, 80, 320, 64)
    frame = _make_synthetic_banner_frame(2560, 1440, roi=roi)

    for raw, expected_verdict in [
        ("yes", VetoVerdict.PASS),
        ("no", VetoVerdict.VETO),
        ("uncertain", VetoVerdict.ABSTAIN),
    ]:
        stub = _StubClient(state={"holds": raw})
        guard = VetoOnlyVLMGuard(state_verifier=RemoteStateVerifier(stub))
        dec = guard.check_state(frame, "alert visible", roi=roi)
        assert dec.verdict is expected_verdict
        assert dec.used_gold_crop is True
        assert max(stub.state_calls[0][0]) <= _MAX_MODEL_IMAGE_DIM
