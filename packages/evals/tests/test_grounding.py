"""Tests for grounding data model and Phase 4 state-narrowing functions.

Covers:
- check_state_preconditions (pre-click state verification)
- verify_transition (post-click transition verification)
- GroundingTarget round-trip serialization
"""

from __future__ import annotations

from openadapt_evals.grounding import (
    GroundingTarget,
    check_state_preconditions,
    verify_transition,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DUMMY_SCREENSHOT = b"\x89PNG\r\n\x1a\n"  # minimal PNG header (content irrelevant)


def _make_ocr_fn(texts: list[str]):
    """Return an ocr_fn that always reports the given texts."""

    def ocr_fn(_screenshot_bytes: bytes) -> list[dict]:
        return [{"text": t, "bbox": [0, 0, 100, 20]} for t in texts]

    return ocr_fn


# ---------------------------------------------------------------------------
# check_state_preconditions
# ---------------------------------------------------------------------------


class TestCheckStatePreconditions:
    """Tests for check_state_preconditions()."""

    def test_no_ocr_returns_true(self):
        """When no OCR function is provided, skip gracefully."""
        target = GroundingTarget(
            window_title="Notepad",
            nearby_text=["File", "Edit"],
        )
        ok, reason = check_state_preconditions(
            DUMMY_SCREENSHOT, target, ocr_fn=None
        )
        assert ok is True
        assert "no OCR available" in reason

    def test_no_expectations_returns_true(self):
        """When target has no text expectations, nothing to check."""
        target = GroundingTarget(description="some button")
        ok, reason = check_state_preconditions(
            DUMMY_SCREENSHOT, target, ocr_fn=_make_ocr_fn([])
        )
        assert ok is True
        assert "no text preconditions" in reason

    def test_with_ocr_window_title_match(self):
        """Window title found on screen => preconditions met."""
        target = GroundingTarget(window_title="Notepad")
        ocr = _make_ocr_fn(["Notepad - Untitled", "File", "Edit"])
        ok, reason = check_state_preconditions(DUMMY_SCREENSHOT, target, ocr)
        assert ok is True
        assert "preconditions met" in reason

    def test_with_ocr_window_title_mismatch(self):
        """Window title NOT found on screen => preconditions fail."""
        target = GroundingTarget(window_title="Notepad")
        ocr = _make_ocr_fn(["Chrome - Settings", "Privacy"])
        ok, reason = check_state_preconditions(DUMMY_SCREENSHOT, target, ocr)
        assert ok is False
        assert "window title mismatch" in reason
        assert "Notepad" in reason

    def test_with_ocr_nearby_text_match(self):
        """Enough nearby_text found => preconditions met."""
        target = GroundingTarget(nearby_text=["File", "Edit", "View", "Help"])
        ocr = _make_ocr_fn(["File", "Edit", "Format"])
        ok, reason = check_state_preconditions(DUMMY_SCREENSHOT, target, ocr)
        assert ok is True  # 2/4 found, threshold is max(1, 4//2) = 2

    def test_with_ocr_nearby_text_mismatch(self):
        """Not enough nearby_text found => preconditions fail."""
        target = GroundingTarget(nearby_text=["File", "Edit", "View", "Help"])
        ocr = _make_ocr_fn(["Settings", "Privacy", "Security"])
        ok, reason = check_state_preconditions(DUMMY_SCREENSHOT, target, ocr)
        assert ok is False
        assert "nearby text mismatch" in reason

    def test_with_ocr_surrounding_labels_match(self):
        """Enough surrounding labels found."""
        target = GroundingTarget(
            surrounding_labels=["OK", "Cancel", "Apply"]
        )
        ocr = _make_ocr_fn(["OK", "Cancel", "Help"])
        ok, reason = check_state_preconditions(DUMMY_SCREENSHOT, target, ocr)
        assert ok is True  # 2/3 found, threshold = max(1, 3//2) = 1

    def test_with_ocr_surrounding_labels_mismatch(self):
        """Not enough surrounding labels found."""
        target = GroundingTarget(
            surrounding_labels=["OK", "Cancel", "Apply"]
        )
        ocr = _make_ocr_fn(["Settings", "Privacy"])
        ok, reason = check_state_preconditions(DUMMY_SCREENSHOT, target, ocr)
        assert ok is False
        assert "surrounding labels mismatch" in reason

    def test_case_insensitive(self):
        """Text matching is case-insensitive by default."""
        target = GroundingTarget(window_title="Notepad")
        ocr = _make_ocr_fn(["NOTEPAD - Untitled"])
        ok, reason = check_state_preconditions(DUMMY_SCREENSHOT, target, ocr)
        assert ok is True

    def test_combined_checks_all_pass(self):
        """Multiple checks all pass."""
        target = GroundingTarget(
            window_title="Notepad",
            nearby_text=["File", "Edit"],
            surrounding_labels=["Format"],
        )
        ocr = _make_ocr_fn(["Notepad", "File", "Edit", "Format", "Help"])
        ok, reason = check_state_preconditions(DUMMY_SCREENSHOT, target, ocr)
        assert ok is True

    def test_combined_checks_title_fails(self):
        """Window title fails even if other checks would pass."""
        target = GroundingTarget(
            window_title="Notepad",
            nearby_text=["File", "Edit"],
        )
        ocr = _make_ocr_fn(["Chrome", "File", "Edit"])
        ok, reason = check_state_preconditions(DUMMY_SCREENSHOT, target, ocr)
        assert ok is False
        assert "window title mismatch" in reason


# ---------------------------------------------------------------------------
# verify_transition
# ---------------------------------------------------------------------------


class TestVerifyTransition:
    """Tests for verify_transition()."""

    def test_no_expectations_returns_true(self):
        """No structured transition expectations => pass."""
        target = GroundingTarget(description="some button")
        ok, reason = verify_transition(DUMMY_SCREENSHOT, target, ocr_fn=None)
        assert ok is True
        assert "no transition expectations" in reason

    def test_no_ocr_returns_true(self):
        """OCR unavailable => skip gracefully."""
        target = GroundingTarget(appearance_text=["Success"])
        ok, reason = verify_transition(DUMMY_SCREENSHOT, target, ocr_fn=None)
        assert ok is True
        assert "no OCR available" in reason

    def test_appearance_text_found(self):
        """Expected text appeared after click."""
        target = GroundingTarget(appearance_text=["Confirmation dialog"])
        ocr = _make_ocr_fn(["Confirmation dialog", "OK", "Cancel"])
        ok, reason = verify_transition(DUMMY_SCREENSHOT, target, ocr)
        assert ok is True
        assert "transition verified" in reason

    def test_appearance_text_missing(self):
        """Expected text did NOT appear."""
        target = GroundingTarget(appearance_text=["Confirmation dialog"])
        ocr = _make_ocr_fn(["Settings", "Privacy"])
        ok, reason = verify_transition(DUMMY_SCREENSHOT, target, ocr)
        assert ok is False
        assert "appearance_text not found" in reason
        assert "Confirmation dialog" in reason

    def test_disappearance_text_gone(self):
        """Text that should vanish is indeed gone."""
        target = GroundingTarget(disappearance_text=["Loading..."])
        ocr = _make_ocr_fn(["Ready", "File", "Edit"])
        ok, reason = verify_transition(DUMMY_SCREENSHOT, target, ocr)
        assert ok is True
        assert "transition verified" in reason

    def test_disappearance_text_still_present(self):
        """Text that should vanish is still visible."""
        target = GroundingTarget(disappearance_text=["Loading..."])
        ocr = _make_ocr_fn(["Loading...", "Please wait"])
        ok, reason = verify_transition(DUMMY_SCREENSHOT, target, ocr)
        assert ok is False
        assert "disappearance_text still present" in reason
        assert "Loading..." in reason

    def test_window_title_change_detected(self):
        """New window title detected after click."""
        target = GroundingTarget(window_title_change="Settings")
        ocr = _make_ocr_fn(["Settings - Chrome", "Privacy"])
        ok, reason = verify_transition(DUMMY_SCREENSHOT, target, ocr)
        assert ok is True

    def test_window_title_change_not_detected(self):
        """New window title NOT detected."""
        target = GroundingTarget(window_title_change="Settings")
        ocr = _make_ocr_fn(["Notepad - Untitled"])
        ok, reason = verify_transition(DUMMY_SCREENSHOT, target, ocr)
        assert ok is False
        assert "window title change not detected" in reason

    def test_modal_toggled_skipped(self):
        """modal_toggled is set but no detection backend — skips."""
        target = GroundingTarget(
            modal_toggled=True,
            appearance_text=["OK"],
        )
        ocr = _make_ocr_fn(["OK", "Cancel"])
        ok, reason = verify_transition(DUMMY_SCREENSHOT, target, ocr)
        assert ok is True  # modal check skipped, appearance_text passes

    def test_combined_appearance_and_disappearance(self):
        """Both appearance and disappearance expectations met."""
        target = GroundingTarget(
            appearance_text=["Saved"],
            disappearance_text=["Saving..."],
        )
        ocr = _make_ocr_fn(["Saved", "File", "Edit"])
        ok, reason = verify_transition(DUMMY_SCREENSHOT, target, ocr)
        assert ok is True

    def test_combined_appearance_passes_disappearance_fails(self):
        """Appearance ok but disappearance still present."""
        target = GroundingTarget(
            appearance_text=["Saved"],
            disappearance_text=["Saving..."],
        )
        ocr = _make_ocr_fn(["Saved", "Saving...", "File"])
        ok, reason = verify_transition(DUMMY_SCREENSHOT, target, ocr)
        assert ok is False
        assert "disappearance_text still present" in reason


# ---------------------------------------------------------------------------
# GroundingTarget round-trip serialization
# ---------------------------------------------------------------------------


class TestGroundingTargetRoundTrip:
    """Tests for to_dict / from_dict serialization."""

    def test_round_trip_preserves_all_fields(self):
        """to_dict() -> from_dict() preserves every non-default field."""
        original = GroundingTarget(
            description="Clear browsing data button",
            target_type="button",
            crop_path="crops/step_03.png",
            crop_bbox=(100, 200, 300, 250),
            click_offset=(50, 25),
            nearby_text=["Clear data", "Browsing history"],
            window_title="Chrome - Settings",
            surrounding_labels=["Cookies", "Cached images"],
            screenshot_before_path="screenshots/before_03.png",
            screenshot_after_path="screenshots/after_03.png",
            disappearance_text=["Clear browsing data"],
            appearance_text=["Your data has been cleared"],
            window_title_change="Chrome - New Tab",
            region_changed=(50, 100, 400, 500),
            modal_toggled=True,
            expected_change="Confirmation dialog appears",
        )

        d = original.to_dict()
        restored = GroundingTarget.from_dict(d)

        assert restored.description == original.description
        assert restored.target_type == original.target_type
        assert restored.crop_path == original.crop_path
        assert restored.crop_bbox == original.crop_bbox
        assert restored.click_offset == original.click_offset
        assert restored.nearby_text == original.nearby_text
        assert restored.window_title == original.window_title
        assert restored.surrounding_labels == original.surrounding_labels
        assert restored.screenshot_before_path == original.screenshot_before_path
        assert restored.screenshot_after_path == original.screenshot_after_path
        assert restored.disappearance_text == original.disappearance_text
        assert restored.appearance_text == original.appearance_text
        assert restored.window_title_change == original.window_title_change
        assert restored.region_changed == original.region_changed
        assert restored.modal_toggled == original.modal_toggled
        assert restored.expected_change == original.expected_change

    def test_round_trip_tuple_from_list(self):
        """JSON serializes tuples as lists; from_dict converts back."""
        d = {
            "description": "test",
            "crop_bbox": [10, 20, 30, 40],
            "click_offset": [5, 5],
            "region_changed": [0, 0, 100, 100],
        }
        restored = GroundingTarget.from_dict(d)
        assert isinstance(restored.crop_bbox, tuple)
        assert isinstance(restored.click_offset, tuple)
        assert isinstance(restored.region_changed, tuple)
        assert restored.crop_bbox == (10, 20, 30, 40)

    def test_round_trip_defaults_omitted(self):
        """Default/empty fields are omitted from to_dict output."""
        target = GroundingTarget(description="test button")
        d = target.to_dict()
        assert "description" in d
        # Empty/default fields should not be present
        assert "nearby_text" not in d
        assert "crop_bbox" not in d
        assert "modal_toggled" not in d
        assert "window_title" not in d

    def test_round_trip_minimal(self):
        """A target with only defaults round-trips cleanly."""
        target = GroundingTarget()
        d = target.to_dict()
        assert d == {}  # everything is default
        restored = GroundingTarget.from_dict(d)
        assert restored.description == ""
        assert restored.nearby_text == []
        assert restored.crop_bbox is None
