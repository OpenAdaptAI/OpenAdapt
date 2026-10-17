"""The global template rung must honor landmark contradiction for LABELED
anchors too (not just unlabeled ones).

A labeled anchor used to be EXEMPT from the landmark-contradiction check on the
theory that its template's baked-in label is discriminative. That assumption
fails on repeated labeled widgets (a "Delete" per row, an "Edit" per card): an
identical labeled look-alike elsewhere can win the full-frame match, and the
old code accepted it unchallenged even when the anchor's own landmarks placed
the target somewhere else. These tests pin the safe behavior: a contradicted
global match falls through instead of resolving to the look-alike.
"""

from __future__ import annotations

import io

from PIL import Image

from openadapt_flow.ir import Anchor, Landmark
from openadapt_flow.runtime.resolver import resolve

VIEWPORT = (640, 480)


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", VIEWPORT, (240, 240, 240)).save(buf, format="PNG")
    return buf.getvalue()


class _Match:
    def __init__(self, point, region, confidence=0.99):
        self.point = point
        self.region = region
        self.confidence = confidence


class _FakeVision:
    """Scripted find_template / find_text (no real cv2/OCR)."""

    def __init__(self, template_results, text_results):
        self._template_results = list(template_results)
        self._text_results = dict(text_results)

    def find_template(
        self,
        screen_png,
        template_png,
        *,
        search_region=None,
        prefer_near=None,
        scales=(0.85, 1.0, 1.18),
        threshold=0.82,
    ):
        if self._template_results:
            return self._template_results.pop(0)
        return None

    def find_text(
        self,
        screen_png,
        text,
        *,
        region=None,
        min_ratio=0.8,
        raise_on_ambiguity=False,
    ):
        del raise_on_ambiguity
        return self._text_results.get(text)


def _labeled_anchor_with_landmark() -> Anchor:
    # Recorded target near (100, 100); a landmark "Name:" sits 200px to its
    # left with an exact offset back to the target.
    return Anchor(
        template="t.png",
        region=(80, 90, 90, 32),
        click_point=(125, 106),
        ocr_text="Delete",
        landmarks=[
            Landmark(
                relation="left_of",
                ocr_text="Name:",
                distance_px=200,
                dx_px=200,
                dy_px=0,
            )
        ],
        search_pad=40,
    )


def test_labeled_global_match_contradicted_by_landmark_falls_through() -> None:
    anchor = _labeled_anchor_with_landmark()
    # Rung 1 (local) misses; rung 2 (global) hits a look-alike far away at
    # ~(500, 400). The located landmark "Name:" at (100, 106) implies the true
    # target is at (300, 106) -- >40px from the global hit -> contradiction.
    vision = _FakeVision(
        template_results=[
            None,  # rung 1 local
            _Match(point=(545, 416), region=(500, 400, 90, 32)),  # rung 2 global
        ],
        text_results={
            "Name:": _Match(point=(100, 106), region=(70, 92, 60, 28), confidence=1.0),
            "Delete": None,  # OCR rung finds no matching label
        },
    )
    result = resolve(anchor, _png(), vision, template_png=b"tmpl")
    assert result is not None
    resolution, _region = result
    # NOT the contradicted look-alike: fell through to the geometry landmark.
    assert resolution.rung == "geometry"
    assert resolution.point == (300, 106)


def test_labeled_global_match_corroborated_by_landmark_is_accepted() -> None:
    anchor = _labeled_anchor_with_landmark()
    # Global hit at (300, 106); landmark at (100, 106) => estimate (300, 106):
    # agrees within tolerance -> accept the global template match.
    vision = _FakeVision(
        template_results=[
            None,  # rung 1 local
            _Match(point=(300, 106), region=(255, 90, 90, 32)),  # rung 2 global
        ],
        text_results={
            "Name:": _Match(point=(100, 106), region=(70, 92, 60, 28), confidence=1.0),
        },
    )
    result = resolve(anchor, _png(), vision, template_png=b"tmpl")
    assert result is not None
    resolution, _region = result
    assert resolution.rung == "template_global"


def test_labeled_anchor_without_landmarks_unchanged() -> None:
    # No landmarks -> contradiction check abstains -> global match accepted
    # (backward-compatible with the pre-fix behavior for this common case).
    anchor = Anchor(
        template="t.png",
        region=(80, 90, 90, 32),
        click_point=(125, 106),
        ocr_text="Delete",
        landmarks=[],
        search_pad=40,
    )
    vision = _FakeVision(
        template_results=[None, _Match(point=(545, 416), region=(500, 400, 90, 32))],
        text_results={},
    )
    result = resolve(anchor, _png(), vision, template_png=b"tmpl")
    assert result is not None
    resolution, _region = result
    assert resolution.rung == "template_global"
