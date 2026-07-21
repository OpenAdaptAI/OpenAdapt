"""Fail-closed OCR targeting for repeated labels and contradictory evidence.

These are small synthetic mechanism tests. They carry no learned thresholds,
deployment data, or application-specific resolution recipes.
"""

from __future__ import annotations

import importlib
import io
from typing import Any

import pytest
from PIL import Image

from openadapt_flow.ir import Anchor, Landmark, Region
from openadapt_flow.runtime.resolver import resolve
from openadapt_flow.vision.match import Match
from openadapt_flow.vision.ocr import (
    AmbiguousOcrMatchError,
    ContradictoryOcrEvidenceError,
    OcrLine,
    find_text,
    find_text_candidates,
)

VIEWPORT = (640, 480)
ocr_module = importlib.import_module("openadapt_flow.vision.ocr")
_AMBIGUOUS = object()


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", VIEWPORT, (240, 240, 240)).save(buffer, format="PNG")
    return buffer.getvalue()


def _match(point: tuple[int, int], region: Region) -> Match:
    return Match(point=point, region=region, confidence=1.0)


def _anchor(*, landmarks: list[Landmark] | None = None) -> Anchor:
    return Anchor(
        template="templates/delete.png",
        region=(80, 90, 90, 32),
        click_point=(125, 106),
        ocr_text="Delete",
        landmarks=landmarks or [],
        search_pad=40,
    )


class _Vision:
    """Region-aware scripted OCR namespace; template matching always misses."""

    def __init__(self, text_results: dict[tuple[str, Region | None], Any]):
        self.text_results = text_results
        self.text_calls: list[tuple[str, Region | None]] = []

    @staticmethod
    def find_template(*_args: Any, **_kwargs: Any) -> None:
        return None

    def find_text(
        self,
        _screen_png: bytes,
        text: str,
        *,
        region: Region | None = None,
        min_ratio: float = 0.8,
        raise_on_ambiguity: bool = False,
    ) -> Match | None:
        del min_ratio
        self.text_calls.append((text, region))
        result = self.text_results.get((text, region))
        if result is _AMBIGUOUS:
            if raise_on_ambiguity:
                raise AmbiguousOcrMatchError("synthetic duplicate OCR lines")
            return None
        return result


class _CandidateVision(_Vision):
    """Scripted namespace exposing the complete target-candidate API."""

    def __init__(
        self,
        *,
        candidates: dict[tuple[str, Region | None], list[Match]],
        text_results: dict[tuple[str, Region | None], Any] | None = None,
    ) -> None:
        super().__init__(text_results or {})
        self.candidates = candidates

    def find_text_candidates(
        self,
        _screen_png: bytes,
        text: str,
        *,
        region: Region | None = None,
        min_ratio: float = 0.8,
    ) -> list[Match]:
        del min_ratio
        self.text_calls.append((text, region))
        return list(self.candidates.get((text, region), []))


class _GlobalTemplateVision(_Vision):
    """Scripted global template candidate followed by OCR evidence."""

    @staticmethod
    def find_template(
        *_args: Any,
        search_region: Region | None = None,
        **_kwargs: Any,
    ) -> Match | None:
        if search_region is not None:
            return None
        return _match((500, 400), (455, 384, 90, 32))


class _Grounder:
    def __init__(self) -> None:
        self.calls = 0

    def locate(self, *_args: Any, **_kwargs: Any) -> Match:
        self.calls += 1
        return _match((600, 440), (580, 430, 40, 20))


def test_find_text_preserves_best_match_for_non_targeting_callers(monkeypatch) -> None:
    """Readiness/presence callers keep the historical best-match behavior."""
    lines = [
        OcrLine(text="Delete", region=(100, 100, 60, 20), confidence=0.99),
        OcrLine(text="Delete", region=(100, 300, 60, 20), confidence=0.98),
    ]
    monkeypatch.setattr(ocr_module, "ocr", lambda *_args, **_kwargs: lines)

    result = find_text(b"synthetic", "Delete", min_ratio=0.9)

    assert result is not None
    assert result.point == (130, 110)


def test_find_text_can_signal_duplicate_qualifying_lines(monkeypatch) -> None:
    """Resolution callers can distinguish ambiguity from an ordinary miss."""
    lines = [
        OcrLine(text="Delete", region=(100, 100, 60, 20), confidence=0.99),
        OcrLine(text="Delete", region=(100, 300, 60, 20), confidence=0.98),
    ]
    monkeypatch.setattr(ocr_module, "ocr", lambda *_args, **_kwargs: lines)

    with pytest.raises(AmbiguousOcrMatchError):
        find_text(
            b"synthetic",
            "Delete",
            min_ratio=0.9,
            raise_on_ambiguity=True,
        )


def test_find_text_candidates_returns_all_without_choosing(monkeypatch) -> None:
    """Candidate enumeration preserves all matches for evidence resolution."""
    lines = [
        OcrLine(text="Delete", region=(100, 300, 60, 20), confidence=0.98),
        OcrLine(text="Delete", region=(100, 100, 60, 20), confidence=0.99),
    ]
    monkeypatch.setattr(ocr_module, "ocr", lambda *_args, **_kwargs: lines)

    result = find_text_candidates(b"synthetic", "Delete", min_ratio=0.9)

    assert [match.point for match in result] == [(130, 310), (130, 110)]


def test_find_text_preserves_unique_success(monkeypatch) -> None:
    """A unique qualifying line still resolves exactly as before."""
    lines = [
        OcrLine(text="Delete", region=(100, 100, 60, 20), confidence=0.99),
        OcrLine(text="Unrelated", region=(100, 300, 80, 20), confidence=0.99),
    ]
    monkeypatch.setattr(ocr_module, "ocr", lambda *_args, **_kwargs: lines)

    result = find_text(b"synthetic", "Delete", min_ratio=0.9)

    assert result is not None
    assert result.point == (130, 110)
    assert result.region == (100, 100, 60, 20)


def test_resolver_uses_local_ocr_before_global_and_short_circuits() -> None:
    """A unique local label wins without consulting the full frame."""
    local_region = (40, 50, 170, 112)
    vision = _Vision({("Delete", local_region): _match((125, 106), (95, 96, 60, 20))})

    result = resolve(_anchor(), _png(), vision, viewport=VIEWPORT)

    assert result is not None
    assert result[0].rung == "ocr"
    assert result[0].point == (125, 106)
    assert vision.text_calls == [("Delete", local_region)]


def test_resolver_uses_global_ocr_only_after_local_miss() -> None:
    """A uniquely moved label remains resolvable after local evidence misses."""
    local_region = (40, 50, 170, 112)
    vision = _Vision(
        {
            ("Delete", local_region): None,
            ("Delete", None): _match((400, 300), (370, 290, 60, 20)),
        }
    )

    result = resolve(_anchor(), _png(), vision, viewport=VIEWPORT)

    assert result is not None
    assert result[0].rung == "ocr"
    assert result[0].point == (400, 300)
    assert vision.text_calls == [("Delete", local_region), ("Delete", None)]


def test_local_ocr_ambiguity_halts_without_weaker_fallback() -> None:
    """Duplicate local labels are a refusal, not a miss or grounding prompt."""
    local_region = (40, 50, 170, 112)
    vision = _Vision({("Delete", local_region): _AMBIGUOUS})
    grounder = _Grounder()

    with pytest.raises(AmbiguousOcrMatchError):
        resolve(_anchor(), _png(), vision, grounder, viewport=VIEWPORT)

    assert vision.text_calls == [("Delete", local_region)]
    assert grounder.calls == 0


def test_global_ocr_ambiguity_halts_without_weaker_fallback() -> None:
    """A local miss may search globally, but global duplicates must halt."""
    local_region = (40, 50, 170, 112)
    vision = _Vision(
        {
            ("Delete", local_region): None,
            ("Delete", None): _AMBIGUOUS,
        }
    )
    grounder = _Grounder()

    with pytest.raises(AmbiguousOcrMatchError):
        resolve(_anchor(), _png(), vision, grounder, viewport=VIEWPORT)

    assert vision.text_calls == [("Delete", local_region), ("Delete", None)]
    assert grounder.calls == 0


@pytest.mark.parametrize("recorded_first", [False, True])
def test_repeated_ocr_target_uses_unique_recorded_region(
    recorded_first: bool,
) -> None:
    """Recorded locality can uniquely identify a repeated target label."""
    local_region = (0, 0, 570, 480)
    recorded = _match((125, 106), (95, 96, 60, 20))
    sibling = _match((400, 300), (370, 290, 60, 20))
    candidates = [recorded, sibling] if recorded_first else [sibling, recorded]
    anchor = _anchor()
    anchor.search_pad = 400
    vision = _CandidateVision(
        candidates={("Delete", local_region): candidates},
    )

    result = resolve(anchor, _png(), vision, viewport=VIEWPORT)

    assert result is not None
    assert result[0].rung == "ocr"
    assert result[0].point == (125, 106)


def test_repeated_ocr_target_without_unique_retained_evidence_halts() -> None:
    """Repeated labels still refuse when neither locality nor context wins."""
    local_region = (40, 50, 170, 112)
    vision = _CandidateVision(
        candidates={
            ("Delete", local_region): [
                _match((60, 70), (30, 60, 60, 20)),
                _match((190, 140), (160, 130, 60, 20)),
            ]
        },
    )
    grounder = _Grounder()

    with pytest.raises(AmbiguousOcrMatchError):
        resolve(_anchor(), _png(), vision, grounder, viewport=VIEWPORT)

    assert grounder.calls == 0


def test_repeated_target_locality_landmark_conflict_halts() -> None:
    """Independent retained evidence selecting different labels is terminal."""
    local_region = (40, 50, 170, 112)
    locality = _match((125, 106), (95, 96, 60, 20))
    landmark_supported = _match((190, 140), (160, 130, 60, 20))
    landmark = Landmark(
        relation="left_of",
        ocr_text="Case A17",
        distance_px=80,
        dx_px=80,
        dy_px=0,
    )
    vision = _CandidateVision(
        candidates={("Delete", local_region): [locality, landmark_supported]},
        text_results={
            ("Case A17", None): _match((110, 140), (80, 130, 60, 20)),
        },
    )

    with pytest.raises(ContradictoryOcrEvidenceError):
        resolve(_anchor(landmarks=[landmark]), _png(), vision, viewport=VIEWPORT)


def test_ambiguous_landmark_halts_instead_of_blind_geometry() -> None:
    """A repeated row landmark cannot be averaged into a coordinate action."""
    anchor = Anchor(
        template="templates/icon.png",
        region=(80, 90, 90, 32),
        click_point=(125, 106),
        landmarks=[
            Landmark(
                relation="left_of",
                ocr_text="Name:",
                distance_px=80,
                dx_px=80,
                dy_px=0,
            )
        ],
    )
    vision = _Vision({("Name:", None): _AMBIGUOUS})
    grounder = _Grounder()

    with pytest.raises(AmbiguousOcrMatchError):
        resolve(anchor, _png(), vision, grounder, viewport=VIEWPORT)

    assert vision.text_calls == [("Name:", None)]
    assert grounder.calls == 0


@pytest.mark.parametrize("ambiguous_first", [False, True])
def test_ambiguous_landmark_abstains_when_unique_landmark_is_sufficient(
    ambiguous_first: bool,
) -> None:
    """One repeated landmark cannot poison independent unique geometry."""
    ambiguous = Landmark(
        relation="left_of",
        ocr_text="Name:",
        distance_px=80,
        dx_px=80,
        dy_px=0,
    )
    unique = Landmark(
        relation="left_of",
        ocr_text="Case A17",
        distance_px=80,
        dx_px=80,
        dy_px=0,
    )
    landmarks = [ambiguous, unique] if ambiguous_first else [unique, ambiguous]
    anchor = Anchor(
        template="templates/icon.png",
        region=(80, 90, 90, 32),
        click_point=(125, 106),
        landmarks=landmarks,
    )
    vision = _Vision(
        {
            ("Name:", None): _AMBIGUOUS,
            ("Case A17", None): _match((100, 106), (70, 96, 60, 20)),
        }
    )
    grounder = _Grounder()

    result = resolve(anchor, _png(), vision, grounder, viewport=VIEWPORT)

    assert result is not None
    assert result[0].rung == "geometry"
    assert result[0].point == (180, 106)
    assert grounder.calls == 0


def test_conflicting_unique_landmarks_refuse_without_grounder() -> None:
    """Unique but inconsistent landmarks cannot be averaged into a click."""
    anchor = Anchor(
        template="templates/icon.png",
        region=(80, 90, 90, 32),
        click_point=(125, 106),
        landmarks=[
            Landmark(
                relation="left_of",
                ocr_text="Case A17",
                distance_px=80,
                dx_px=80,
                dy_px=0,
            ),
            Landmark(
                relation="above",
                ocr_text="Account 42",
                distance_px=60,
                dx_px=0,
                dy_px=60,
            ),
        ],
    )
    vision = _Vision(
        {
            ("Case A17", None): _match((100, 106), (70, 96, 60, 20)),
            ("Account 42", None): _match((400, 300), (360, 290, 80, 20)),
        }
    )
    grounder = _Grounder()

    with pytest.raises(ContradictoryOcrEvidenceError):
        resolve(anchor, _png(), vision, grounder, viewport=VIEWPORT)

    assert grounder.calls == 0


@pytest.mark.parametrize("valid_first", [False, True])
def test_geometry_uses_unique_in_region_estimate_not_stale_outlier(
    valid_first: bool,
) -> None:
    """Recorded locality selects one estimate; incompatible points aren't averaged."""
    valid = Landmark(
        relation="left_of",
        ocr_text="Consult",
        distance_px=25,
        dx_px=25,
        dy_px=0,
    )
    stale = Landmark(
        relation="above",
        ocr_text="Save Encounter",
        distance_px=60,
        dx_px=0,
        dy_px=60,
    )
    landmarks = [valid, stale] if valid_first else [stale, valid]
    anchor = Anchor(
        template="templates/field.png",
        region=(80, 90, 90, 32),
        click_point=(125, 106),
        landmarks=landmarks,
    )
    vision = _Vision(
        {
            ("Consult", None): _match((100, 106), (70, 96, 60, 20)),
            ("Save Encounter", None): _match(
                (400, 300),
                (350, 290, 100, 20),
            ),
        }
    )

    result = resolve(anchor, _png(), vision, viewport=VIEWPORT)

    assert result is not None
    assert result[0].rung == "geometry"
    assert result[0].point == (125, 106)


@pytest.mark.parametrize("old_region_first", [False, True])
def test_geometry_refuses_old_region_singleton_vs_current_landmark_cluster(
    old_region_first: bool,
) -> None:
    """A stale local singleton cannot silently beat two agreeing relations."""
    old = Landmark(
        relation="left_of",
        ocr_text="Old context",
        distance_px=25,
        dx_px=25,
        dy_px=0,
    )
    current_a = Landmark(
        relation="left_of",
        ocr_text="Current A",
        distance_px=80,
        dx_px=80,
        dy_px=0,
    )
    current_b = Landmark(
        relation="above",
        ocr_text="Current B",
        distance_px=60,
        dx_px=0,
        dy_px=60,
    )
    landmarks = (
        [old, current_a, current_b] if old_region_first else [current_b, current_a, old]
    )
    anchor = Anchor(
        template="templates/field.png",
        region=(80, 90, 90, 32),
        click_point=(125, 106),
        landmarks=landmarks,
    )
    vision = _Vision(
        {
            ("Old context", None): _match((100, 106), (70, 96, 60, 20)),
            ("Current A", None): _match((320, 300), (290, 290, 60, 20)),
            ("Current B", None): _match((402, 242), (372, 232, 60, 20)),
        }
    )
    grounder = _Grounder()

    with pytest.raises(ContradictoryOcrEvidenceError):
        resolve(anchor, _png(), vision, grounder, viewport=VIEWPORT)

    assert grounder.calls == 0


def test_unique_ocr_target_survives_stale_conflicting_landmark_geometry() -> None:
    """A unique observed label is not vetoed by stale fixed-offset context."""
    local_region = (40, 50, 170, 112)
    anchor = _anchor(
        landmarks=[
            Landmark(
                relation="left_of",
                ocr_text="Case A17",
                distance_px=25,
                dx_px=25,
                dy_px=0,
            ),
            Landmark(
                relation="above",
                ocr_text="Account 42",
                distance_px=60,
                dx_px=0,
                dy_px=60,
            ),
        ]
    )
    vision = _Vision(
        {
            ("Delete", local_region): _match((125, 106), (95, 96, 60, 20)),
            ("Case A17", None): _match((100, 106), (70, 96, 60, 20)),
            ("Account 42", None): _match((400, 300), (360, 290, 80, 20)),
        }
    )

    result = resolve(anchor, _png(), vision, viewport=VIEWPORT)

    assert result is not None
    assert result[0].rung == "ocr"
    assert result[0].point == (125, 106)


def test_conflicting_landmarks_reject_template_but_not_unique_target_ocr() -> None:
    """Stale context falls through instead of vetoing unique target text."""
    local_region = (40, 50, 170, 112)
    anchor = _anchor(
        landmarks=[
            Landmark(
                relation="left_of",
                ocr_text="Case A17",
                distance_px=25,
                dx_px=25,
                dy_px=0,
            ),
            Landmark(
                relation="above",
                ocr_text="Account 42",
                distance_px=60,
                dx_px=0,
                dy_px=60,
            ),
        ]
    )
    vision = _GlobalTemplateVision(
        {
            ("Case A17", None): _match((100, 106), (70, 96, 60, 20)),
            ("Account 42", None): _match((400, 300), (360, 290, 80, 20)),
            ("Delete", local_region): _match((125, 106), (95, 96, 60, 20)),
        }
    )

    result = resolve(
        anchor,
        _png(),
        vision,
        template_png=b"scripted",
        viewport=VIEWPORT,
    )

    assert result is not None
    assert result[0].rung == "ocr"
    assert result[0].point == (125, 106)


@pytest.mark.parametrize("ambiguous_first", [False, True])
def test_ambiguous_landmark_abstains_from_unique_target_corroboration(
    ambiguous_first: bool,
) -> None:
    """Repeated context does not veto a target corroborated by unique context."""
    local_region = (40, 50, 170, 112)
    ambiguous = Landmark(
        relation="left_of",
        ocr_text="Name:",
        distance_px=80,
        dx_px=80,
        dy_px=0,
    )
    unique = Landmark(
        relation="left_of",
        ocr_text="Case A17",
        distance_px=25,
        dx_px=25,
        dy_px=0,
    )
    landmarks = [ambiguous, unique] if ambiguous_first else [unique, ambiguous]
    vision = _Vision(
        {
            ("Delete", local_region): _match((125, 106), (95, 96, 60, 20)),
            ("Name:", None): _AMBIGUOUS,
            ("Case A17", None): _match((100, 106), (70, 96, 60, 20)),
        }
    )

    result = resolve(_anchor(landmarks=landmarks), _png(), vision, viewport=VIEWPORT)

    assert result is not None
    assert result[0].rung == "ocr"
    assert result[0].point == (125, 106)


def test_unique_moved_global_label_survives_stale_landmark_geometry() -> None:
    """A unique moved label wins when no competing observed target exists."""
    local_region = (40, 50, 170, 112)
    landmark = Landmark(
        relation="left_of",
        ocr_text="Name:",
        distance_px=200,
        dx_px=200,
        dy_px=0,
    )
    vision = _Vision(
        {
            ("Delete", local_region): None,
            ("Delete", None): _match((545, 416), (515, 406, 60, 20)),
            ("Name:", None): _match((100, 106), (70, 96, 60, 20)),
        }
    )

    result = resolve(_anchor(landmarks=[landmark]), _png(), vision, viewport=VIEWPORT)

    assert result is not None
    assert result[0].rung == "ocr"
    assert result[0].point == (545, 416)
    assert vision.text_calls == [
        ("Delete", local_region),
        ("Delete", None),
    ]


def test_unique_local_label_survives_stale_landmark_geometry() -> None:
    """Local unique target presence outranks an obsolete recorded offset."""
    local_region = (40, 50, 170, 112)
    landmark = Landmark(
        relation="left_of",
        ocr_text="Name:",
        distance_px=80,
        dx_px=80,
        dy_px=0,
    )
    vision = _Vision(
        {
            ("Delete", local_region): _match((190, 140), (160, 130, 60, 20)),
            ("Name:", None): _match((50, 60), (20, 50, 60, 20)),
        }
    )

    result = resolve(_anchor(landmarks=[landmark]), _png(), vision, viewport=VIEWPORT)

    assert result is not None
    assert result[0].rung == "ocr"
    assert result[0].point == (190, 140)
    assert vision.text_calls == [("Delete", local_region)]
