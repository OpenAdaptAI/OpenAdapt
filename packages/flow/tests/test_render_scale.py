"""Display-scale estimate and the scaled backend view (runtime.render_scale)."""

from __future__ import annotations

import hashlib
import io
import json
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from openadapt_flow.backend import (
    GuardedCoordinateActionBackend,
    RemoteActuationBackend,
    StructuralActionBackend,
)
from openadapt_flow.ir import Anchor
from openadapt_flow.runtime import layout_drift as drift
from openadapt_flow.runtime import render_scale as rs
from openadapt_flow.runtime.identity import text_render_differs
from openadapt_flow.runtime.resolver import resolve
from openadapt_flow.vision.ocr import AmbiguousOcrMatchError

RECORDED = (1280, 800)
LABELS = [
    (482.5, 56.5),
    (136.5, 131.5),
    (657.5, 226.5),
    (610.0, 346.0),
    (617.5, 465.5),
    (737.5, 593.0),
    (150.0, 263.0),
]


def _matches(scale: float, offset=(0.0, 0.0), jitter=0.0, labels=LABELS):
    rng = np.random.default_rng(7)
    out = []
    for x, y in labels:
        jx, jy = rng.uniform(-jitter, jitter, 2)
        out.append(
            rs.LandmarkMatch(
                recorded=(x, y),
                live=(scale * x + offset[0] + jx, scale * y + offset[1] + jy),
            )
        )
    return out


def _viewport(scale: float) -> tuple[int, int]:
    return (round(RECORDED[0] * scale), round(RECORDED[1] * scale))


@pytest.mark.parametrize("scale", [1.25, 1.5, 1.75, 0.8])
def test_whole_session_scale_snaps_to_the_frame_ratio(scale: float) -> None:
    transform, reason = rs.estimate_render_transform(
        _matches(scale, jitter=3.0),
        recorded_viewport=RECORDED,
        live_viewport=_viewport(scale),
    )
    assert transform is not None, reason
    assert transform.scale == pytest.approx(scale, abs=1e-3)
    assert transform.offset == (0.0, 0.0)
    assert transform.landmarks_used == len(LABELS)


def test_scaled_window_inside_a_larger_desktop_keeps_its_offset() -> None:
    transform, reason = rs.estimate_render_transform(
        _matches(1.25, offset=(40.0, 30.0), jitter=2.0),
        recorded_viewport=RECORDED,
        live_viewport=(1920, 1200),
    )
    assert transform is not None, reason
    assert transform.scale == pytest.approx(1.25, abs=5e-3)
    assert transform.offset == pytest.approx((40.0, 30.0), abs=3.0)
    assert transform.to_live(100, 100) == pytest.approx((165, 155), abs=3)


def test_one_moved_label_does_not_steer_the_fit() -> None:
    matches = _matches(1.5, jitter=1.0)
    moved = matches[2]
    matches[2] = rs.LandmarkMatch(
        recorded=moved.recorded, live=(moved.live[0] + 200, moved.live[1] - 90)
    )
    transform, reason = rs.estimate_render_transform(
        matches, recorded_viewport=RECORDED, live_viewport=_viewport(1.5)
    )
    assert transform is not None, reason
    assert transform.scale == pytest.approx(1.5, abs=1e-3)
    assert transform.landmarks_used == len(LABELS) - 1


def test_recorded_geometry_is_not_a_transform() -> None:
    transform, reason = rs.estimate_render_transform(
        _matches(1.0, jitter=2.0), recorded_viewport=RECORDED, live_viewport=RECORDED
    )
    assert transform is None
    assert reason == "recorded geometry"


def test_too_few_labels_gives_no_estimate() -> None:
    transform, reason = rs.estimate_render_transform(
        _matches(1.25)[:2], recorded_viewport=RECORDED, live_viewport=_viewport(1.25)
    )
    assert transform is None
    assert "need 3" in reason


def test_clustered_labels_give_no_estimate() -> None:
    cluster = [(600.0, 300.0), (640.0, 330.0), (610.0, 360.0), (650.0, 380.0)]
    transform, reason = rs.estimate_render_transform(
        _matches(1.25, labels=cluster),
        recorded_viewport=RECORDED,
        live_viewport=_viewport(1.25),
    )
    assert transform is None
    assert "close together" in reason or "span" in reason


def test_axes_that_scale_differently_are_not_a_display_scale() -> None:
    matches = [
        rs.LandmarkMatch(recorded=(x, y), live=(1.25 * x, 1.0 * y)) for x, y in LABELS
    ]
    transform, reason = rs.estimate_render_transform(
        matches, recorded_viewport=RECORDED, live_viewport=(1600, 800)
    )
    assert transform is None


def test_recorded_frame_must_fit_inside_the_live_frame() -> None:
    transform, reason = rs.estimate_render_transform(
        _matches(1.5), recorded_viewport=RECORDED, live_viewport=RECORDED
    )
    assert transform is None
    assert "does not fit" in reason


# --------------------------------------------------------------- landmarks


def _line(text: str, region, confidence: float = 0.95):
    return SimpleNamespace(text=text, region=region, confidence=confidence)


def test_landmark_set_stores_no_text_and_skips_params_and_repeats() -> None:
    salt = "00" * 16
    lines = [
        _line("Patient roster", (50, 120, 170, 24)),
        _line("Visit type", (550, 330, 120, 24)),
        _line("Save", (550, 570, 60, 24)),
        _line("Save", (950, 570, 60, 24)),
        _line("Cardiology follow-up", (550, 370, 260, 24)),
        _line("Request ID", (550, 450, 135, 24), confidence=0.4),
    ]
    entries = rs.scale_landmark_set(
        lines, salt_hex=salt, exclude_texts=["Cardiology follow-up"]
    )
    assert len(entries) == 2  # roster + visit type
    blob = json.dumps(entries)
    for text in ("roster", "Visit", "Save", "Cardiology"):
        assert text.lower() not in blob.lower()
    assert entries[0][1:] == [135.0, 132.0, 170, 24]


def test_hashed_labels_match_through_glyph_confusion() -> None:
    salt = "ab" * 16
    entries = rs.scale_landmark_set(
        [_line("Request ID 1001", (550, 450, 200, 24))], salt_hex=salt
    )
    live = [_line("Request lD l00l", (700, 560, 250, 30))]
    matches = rs.match_landmarks(rs.hashed_landmarks(entries), live, salt_hex=salt)
    assert len(matches) == 1
    assert matches[0].live == (825.0, 575.0)


def test_fingerprint_asset_round_trips_scale_landmarks() -> None:
    fp = drift.LayoutFingerprint((1280, 800), 24.0, 10.0, 5, {})
    raw = drift.dump_bundle_fingerprints(
        {"step_000": fp},
        scale_landmarks={
            "salt": "cd" * 16,
            "steps": {"step_000": [["abc", 10.0, 20.0, 30, 12]], "gone": []},
        },
    )
    salt, steps = drift.load_scale_landmarks(raw)
    assert salt == "cd" * 16
    assert steps == {"step_000": [["abc", 10.0, 20.0, 30.0, 12.0]]}
    assert drift.load_bundle_fingerprints(raw)["step_000"] == fp
    # Older bundles carry no landmark set.
    assert drift.load_scale_landmarks(drift.dump_bundle_fingerprints({"s": fp})) == (
        "",
        {},
    )


def test_normalized_frame_check_uses_label_widths() -> None:
    salt = "ef" * 16
    rec = [
        _line("Patient roster", (50, 120, 170, 24)),
        _line("Appointment slot", (550, 210, 210, 24)),
        _line("Visit type", (550, 330, 120, 24)),
        _line("Request ID", (550, 450, 135, 24)),
    ]
    entries = rs.scale_landmark_set(rec, salt_hex=salt)
    kwargs = dict(salt_hex=salt, text_height_ratio=0.08, landmark_px=12)
    # Same text; OCR boxes 3 px taller or shorter: no drift.
    jittered = [
        _line(
            line.text,
            (
                line.region[0],
                line.region[1] - 1,
                line.region[2],
                line.region[3] + (3 if i % 2 else -3),
            ),
        )
        for i, line in enumerate(rec)
    ]
    assert rs.check_normalized_frame(entries, jittered, **kwargs) == []
    # Fonts 20% larger, centred where they were: every label is wider. (Left
    # anchored, the centres move too and the position test reports it.)
    wider = [
        _line(
            line.text,
            (
                line.region[0] - line.region[2] // 10,
                line.region[1],
                line.region[2] + 2 * (line.region[2] // 10),
                line.region[3],
            ),
        )
        for line in rec
    ]
    details = rs.check_normalized_frame(entries, wider, **kwargs)
    assert details and "recorded width" in details[0]
    # Too few labels with sizes: the caller falls back to the fingerprint.
    assert rs.check_normalized_frame(entries[:2], rec, **kwargs) is None


# ----------------------------------------------------------- scaled backend


def _png(width: int, height: int, colour=(240, 240, 240)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, format="PNG")
    return buffer.getvalue()


class _Guarded:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.frame = _png(1600, 1000)

    viewport = (1600, 1000)

    def screenshot(self) -> bytes:
        return self.frame

    def click(self, x, y, *, double=False):
        self.calls.append(("click", x, y))

    def type_text(self, text):
        self.calls.append(("type", text))

    def press(self, key):
        self.calls.append(("press", key))

    def scroll(self, dx, dy):
        self.calls.append(("scroll", dx, dy))

    def arm_guarded_coordinate(self, x, y):
        self.calls.append(("arm", x, y))

    def cancel_guarded_coordinate(self):
        self.calls.append(("cancel",))

    def act_guarded_coordinate(
        self, x, y, *, expected_frame_sha256, double=False, button="left"
    ):
        assert expected_frame_sha256 == hashlib.sha256(self.frame).hexdigest()
        self.calls.append(("act", x, y))
        return "receipt"

    # A structural capability the scaled view must not pass through.
    def locate_structural(self, locator):
        raise AssertionError("structural lookup must be hidden")

    def act_structural(self, locator, handle, *, double=False):
        raise AssertionError("structural actuation must be hidden")


def _transform(scale=1.25):
    return rs.RenderTransform(
        scale=scale,
        offset=(0.0, 0.0),
        recorded_viewport=RECORDED,
        live_viewport=_viewport(scale),
        landmarks_used=5,
        landmarks_located=5,
        max_residual_px=1.0,
    )


def test_scaled_view_exposes_only_what_it_can_map() -> None:
    inner = _Guarded()
    view = rs.render_scaled_backend(inner, _transform())
    assert isinstance(view, GuardedCoordinateActionBackend)
    assert not isinstance(view, StructuralActionBackend)
    assert not isinstance(view, RemoteActuationBackend)
    assert not hasattr(view, "locate_structural")
    assert view.viewport == RECORDED


def test_scaled_view_maps_coordinates_and_frames() -> None:
    inner = _Guarded()
    view = rs.render_scaled_backend(inner, _transform())
    frame = view.screenshot()
    with Image.open(io.BytesIO(frame)) as img:
        assert img.size == RECORDED
    view.click(400, 200)
    view.scroll(0, 100)
    view.arm_guarded_coordinate(400, 200)
    assert (
        view.act_guarded_coordinate(
            400, 200, expected_frame_sha256=hashlib.sha256(frame).hexdigest()
        )
        == "receipt"
    )
    assert inner.calls == [
        ("click", 500, 250),
        ("scroll", 0, 125),
        ("arm", 500, 250),
        ("act", 500, 250),
    ]


def test_guarded_action_on_an_unseen_frame_is_refused() -> None:
    view = rs.render_scaled_backend(_Guarded(), _transform())
    view.screenshot()
    with pytest.raises(rs.RenderScaleError):
        view.act_guarded_coordinate(1, 1, expected_frame_sha256="0" * 64)


def test_frame_size_change_after_the_estimate_is_refused() -> None:
    inner = _Guarded()
    view = rs.render_scaled_backend(inner, _transform())
    inner.frame = _png(1920, 1200)
    with pytest.raises(rs.RenderScaleError):
        view.screenshot()


# ------------------------------------------------- font-tolerant OCR rung


class _Vision:
    def __init__(self, lines):
        self._lines = lines

    def find_template(self, *args, **kwargs):
        return None

    def find_text_candidates(self, screen, text, *, region=None, min_ratio=0.8):
        return []

    def find_text(self, *args, **kwargs):
        return None

    def ocr(self, png, region=None):
        return self._lines


def _anchor() -> Anchor:
    return Anchor(
        template="templates/step_000.png",
        region=(252, 176, 149, 32),
        click_point=(326, 192),
        ocr_text="MRN A1001",
        landmarks=[],
        search_pad=40,
    )


def test_ocr_rung_reads_a_label_through_glyph_confusion() -> None:
    vision = _Vision([_line("MRN Al00l", (255, 179, 143, 27))])
    result = resolve(_anchor(), _png(1280, 800), vision, viewport=RECORDED)
    assert result is not None
    resolution, _region = result
    assert resolution.rung == "ocr"
    assert resolution.point == (326, 192)


def test_glyph_tolerant_match_only_counts_where_the_target_was_recorded() -> None:
    vision = _Vision([_line("MRN Al00l", (900, 600, 143, 27))])
    assert resolve(_anchor(), _png(1280, 800), vision, viewport=RECORDED) is None


def test_two_local_glyph_tolerant_matches_refuse() -> None:
    vision = _Vision(
        [
            _line("MRN Al00l", (255, 170, 143, 20)),
            _line("MRN A100l", (255, 200, 143, 20)),
        ]
    )
    with pytest.raises(AmbiguousOcrMatchError):
        resolve(_anchor(), _png(1280, 800), vision, viewport=RECORDED)


def test_glyph_tolerance_does_not_accept_a_different_label() -> None:
    vision = _Vision([_line("MRN B2002", (255, 179, 143, 27))])
    assert resolve(_anchor(), _png(1280, 800), vision, viewport=RECORDED) is None


# --------------------------------------------- pixel identity, render change


def _text_crop(cleartype: bool) -> bytes:
    from PIL import ImageDraw, ImageFont

    image = Image.new("RGB", (240, 40), (255, 255, 255))
    if cleartype:
        big = Image.new("L", (720, 40), 0)
        ImageDraw.Draw(big).text(
            (12, 8), "MRN 24682", fill=255, font=ImageFont.load_default(size=24)
        )
        cov = np.asarray(big, dtype=np.float32) / 255.0
        cov = cov.reshape(40, 240, 3)
        arr = (255 * (1 - cov)).astype(np.uint8)
        image = Image.fromarray(arr, "RGB")
    else:
        draw = ImageDraw.Draw(image)
        draw.fontmode = "1"
        draw.text(
            (4, 8), "MRN 24682", fill=(0, 0, 0), font=ImageFont.load_default(size=24)
        )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_cleartype_toggle_is_a_render_change() -> None:
    on, off = _text_crop(True), _text_crop(False)
    assert text_render_differs(on, off)
    assert text_render_differs(off, on)
    assert not text_render_differs(on, on)
    assert not text_render_differs(off, off)


def test_battery_renders_are_not_a_render_change() -> None:
    from openadapt_flow.validation import pixel_identity_aligned as battery

    for target, sibling in battery.COLLAPSE_PAIRS:
        rec = battery._to_png(battery.render_mrn(target))
        for live in (
            battery.render_mrn(sibling),
            battery._jpeg(
                battery._subpixel_shift(battery.render_mrn(target), 0.8, 0.6), 18
            ),
            battery._scale(battery.render_mrn(sibling), 1.5),
            255 - battery.render_mrn(sibling),
        ):
            assert not text_render_differs(rec, battery._to_png(live))
