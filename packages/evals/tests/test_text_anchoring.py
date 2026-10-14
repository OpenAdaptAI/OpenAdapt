"""Tests for Phase 5 OCR text anchoring (Tier 1.5a).

Covers:
- run_ocr graceful fallback when pytesseract is not installed
- ground_by_text scoring tiers (exact, case-insensitive, substring, fuzzy)
- ground_by_text nearby-text proximity boost
- ground_by_text sorting and top-5 limit
- DemoExecutor._try_text_anchoring integration (with mocked OCR)
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from openadapt_evals.grounding import (
    GroundingTarget,
    _bbox_center,
    _bbox_distance,
    _char_overlap_ratio,
    ground_by_text,
    run_ocr,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DUMMY_SCREENSHOT = b"\x89PNG\r\n\x1a\n"  # minimal PNG header


def _make_ocr_results(entries: list[tuple[str, list[int]]]) -> list[dict]:
    """Build mock OCR results from (text, bbox) pairs."""
    return [
        {"text": text, "bbox": bbox, "confidence": 0.95}
        for text, bbox in entries
    ]


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestHelperFunctions:
    """Tests for _char_overlap_ratio, _bbox_center, _bbox_distance."""

    def test_char_overlap_ratio_identical(self):
        assert _char_overlap_ratio("hello", "hello") == 1.0

    def test_char_overlap_ratio_empty(self):
        assert _char_overlap_ratio("", "hello") == 0.0
        assert _char_overlap_ratio("hello", "") == 0.0

    def test_char_overlap_ratio_partial(self):
        ratio = _char_overlap_ratio("abcde", "abcfg")
        # overlap: a, b, c -> 3; max_len = 5 -> 0.6
        assert ratio == pytest.approx(0.6)

    def test_char_overlap_ratio_case_insensitive(self):
        assert _char_overlap_ratio("Hello", "HELLO") == 1.0

    def test_bbox_center(self):
        cx, cy = _bbox_center([10, 20, 30, 40])
        assert cx == 20.0
        assert cy == 30.0

    def test_bbox_distance_same(self):
        assert _bbox_distance([0, 0, 10, 10], [0, 0, 10, 10]) == 0.0

    def test_bbox_distance_known(self):
        # Centers: (5, 5) and (8, 9) -> distance = sqrt(9+16) = 5.0
        dist = _bbox_distance([0, 0, 10, 10], [6, 8, 10, 10])
        assert dist == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# run_ocr
# ---------------------------------------------------------------------------


class TestRunOCR:
    """Tests for run_ocr()."""

    def test_run_ocr_no_pytesseract(self):
        """When pytesseract is not installed, returns empty list."""
        with patch.dict("sys.modules", {"pytesseract": None}):
            result = run_ocr(DUMMY_SCREENSHOT)
        assert result == []


# ---------------------------------------------------------------------------
# ground_by_text
# ---------------------------------------------------------------------------


class TestGroundByText:
    """Tests for ground_by_text()."""

    def test_ground_by_text_exact_match(self):
        """Exact text match scores 0.95."""
        ocr = _make_ocr_results([("Save", [100, 200, 150, 220])])
        target = GroundingTarget(description="Save")

        candidates = ground_by_text(DUMMY_SCREENSHOT, target, ocr_results=ocr)

        assert len(candidates) == 1
        assert candidates[0].local_score == 0.95
        assert candidates[0].matched_text == "Save"
        assert candidates[0].source == "ocr"

    def test_ground_by_text_case_insensitive(self):
        """Case-insensitive match scores 0.90."""
        ocr = _make_ocr_results([("save", [100, 200, 150, 220])])
        target = GroundingTarget(description="Save")

        candidates = ground_by_text(DUMMY_SCREENSHOT, target, ocr_results=ocr)

        assert len(candidates) == 1
        assert candidates[0].local_score == 0.90

    def test_ground_by_text_substring(self):
        """Substring match scores 0.70."""
        ocr = _make_ocr_results([
            ("Save As...", [100, 200, 200, 220]),
        ])
        target = GroundingTarget(description="Save")

        candidates = ground_by_text(DUMMY_SCREENSHOT, target, ocr_results=ocr)

        assert len(candidates) == 1
        assert candidates[0].local_score == 0.70

    def test_ground_by_text_no_match(self):
        """No matching text returns empty list."""
        ocr = _make_ocr_results([
            ("File", [10, 10, 50, 30]),
            ("Edit", [60, 10, 100, 30]),
        ])
        target = GroundingTarget(description="Export")

        candidates = ground_by_text(DUMMY_SCREENSHOT, target, ocr_results=ocr)

        assert candidates == []

    def test_ground_by_text_sorted_by_score(self):
        """Candidates are sorted by score descending."""
        ocr = _make_ocr_results([
            ("save as", [200, 200, 300, 220]),    # substring of "Save" reversed -> substring match
            ("Save", [100, 200, 150, 220]),        # exact match -> 0.95
            ("SAVE", [300, 200, 350, 220]),         # case-insensitive -> 0.90
        ])
        target = GroundingTarget(description="Save")

        candidates = ground_by_text(DUMMY_SCREENSHOT, target, ocr_results=ocr)

        assert len(candidates) == 3
        scores = [c.local_score for c in candidates]
        assert scores == sorted(scores, reverse=True)
        # Exact match should be first
        assert candidates[0].local_score == 0.95
        assert candidates[0].matched_text == "Save"

    def test_ground_by_text_nearby_boost(self):
        """Candidates near nearby_text locations get +0.05 boost."""
        # "OK" button near "Confirm" label
        ocr = _make_ocr_results([
            ("OK", [100, 200, 140, 220]),
            ("Confirm", [90, 170, 180, 190]),
        ])
        target = GroundingTarget(
            description="OK",
            nearby_text=["Confirm"],
        )

        candidates = ground_by_text(DUMMY_SCREENSHOT, target, ocr_results=ocr)

        assert len(candidates) == 1
        # Exact match (0.95) + nearby boost (0.05) = 1.0 (capped)
        assert candidates[0].local_score == 1.0
        assert "nearby boost" in candidates[0].reasoning

    def test_ground_by_text_no_nearby_boost_when_far(self):
        """Candidates far from nearby_text do NOT get boosted."""
        ocr = _make_ocr_results([
            ("OK", [100, 200, 140, 220]),
            ("Confirm", [2000, 2000, 2100, 2020]),  # very far away
        ])
        target = GroundingTarget(
            description="OK",
            nearby_text=["Confirm"],
        )

        candidates = ground_by_text(DUMMY_SCREENSHOT, target, ocr_results=ocr)

        assert len(candidates) == 1
        # No boost -- stays at exact match score
        assert candidates[0].local_score == 0.95

    def test_ground_by_text_with_mock_ocr(self):
        """Integration test: ground_by_text with ocr_results=None uses run_ocr."""
        mock_results = _make_ocr_results([
            ("Submit", [400, 500, 480, 520]),
        ])
        target = GroundingTarget(description="Submit")

        with patch(
            "openadapt_evals.grounding.run_ocr", return_value=mock_results
        ):
            candidates = ground_by_text(
                DUMMY_SCREENSHOT, target, ocr_results=None
            )

        assert len(candidates) == 1
        assert candidates[0].local_score == 0.95
        assert candidates[0].matched_text == "Submit"
        cx, cy = _bbox_center([400, 500, 480, 520])
        assert candidates[0].point == (int(cx), int(cy))

    def test_ground_by_text_empty_description(self):
        """Empty description returns empty list."""
        ocr = _make_ocr_results([("Save", [100, 200, 150, 220])])
        target = GroundingTarget(description="")

        candidates = ground_by_text(DUMMY_SCREENSHOT, target, ocr_results=ocr)
        assert candidates == []

    def test_ground_by_text_empty_ocr(self):
        """Empty OCR results returns empty list."""
        target = GroundingTarget(description="Save")
        candidates = ground_by_text(DUMMY_SCREENSHOT, target, ocr_results=[])
        assert candidates == []

    def test_ground_by_text_top_5_limit(self):
        """At most 5 candidates returned."""
        ocr = _make_ocr_results([
            (f"Save {i}", [i * 50, 0, i * 50 + 40, 20])
            for i in range(10)
        ])
        target = GroundingTarget(description="Save")

        candidates = ground_by_text(DUMMY_SCREENSHOT, target, ocr_results=ocr)

        assert len(candidates) <= 5

    def test_ground_by_text_point_is_bbox_center(self):
        """Candidate point is the center of the matched bbox."""
        bbox = [100, 200, 300, 400]
        ocr = _make_ocr_results([("Click Me", bbox)])
        target = GroundingTarget(description="Click Me")

        candidates = ground_by_text(DUMMY_SCREENSHOT, target, ocr_results=ocr)

        assert len(candidates) == 1
        expected_cx, expected_cy = _bbox_center(bbox)
        assert candidates[0].point == (int(expected_cx), int(expected_cy))

    def test_ground_by_text_fuzzy_match(self):
        """Fuzzy match (>80% char overlap) scores 0.50."""
        # "Savee" vs "Save" -> overlap: S,a,v,e = 4 chars; max_len=5 -> 0.8
        # Need >0.80, so try "Savee" (5 chars) vs "Savef" (5 chars)
        # Actually let's use a better example: "Settings" vs "Settingx"
        # overlap: S,e,t,t,i,n,g = 7; max_len = 8 -> 0.875 > 0.80
        ocr = _make_ocr_results([("Settingx", [100, 200, 200, 220])])
        target = GroundingTarget(description="Settings")

        candidates = ground_by_text(DUMMY_SCREENSHOT, target, ocr_results=ocr)

        assert len(candidates) == 1
        assert candidates[0].local_score == 0.50
