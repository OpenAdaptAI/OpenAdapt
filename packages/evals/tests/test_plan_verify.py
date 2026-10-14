"""Tests for openadapt_evals.plan_verify -- VLM-based step verification."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from openadapt_evals.plan_verify import (
    VerificationResult,
    _parse_verification_result,
    verify_goal_completion,
    verify_plan_progress,
    verify_step,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FAKE_SCREENSHOT = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100  # minimal PNG-ish bytes


# ---------------------------------------------------------------------------
# VerificationResult dataclass
# ---------------------------------------------------------------------------


class TestVerificationResult:
    """Tests for VerificationResult creation and validation."""

    def test_create_verified(self):
        r = VerificationResult(
            status="verified",
            confidence=0.95,
            explanation="Cell A1 contains 'Year'",
            raw_response='{"status":"verified","confidence":0.95}',
        )
        assert r.status == "verified"
        assert r.confidence == 0.95
        assert "Year" in r.explanation

    def test_create_not_verified(self):
        r = VerificationResult(
            status="not_verified",
            confidence=0.8,
            explanation="Cell A1 is empty",
            raw_response="{}",
        )
        assert r.status == "not_verified"
        assert r.confidence == 0.8

    def test_create_unclear(self):
        r = VerificationResult(
            status="unclear",
            confidence=0.3,
            explanation="Cannot determine",
            raw_response="",
        )
        assert r.status == "unclear"

    def test_create_partially_verified(self):
        r = VerificationResult(
            status="partially_verified",
            confidence=0.85,
            explanation="Text present in correct cell, but cursor moved",
            raw_response="{}",
        )
        assert r.status == "partially_verified"
        assert r.confidence == 0.85

    def test_invalid_status_raises(self):
        with pytest.raises(ValueError, match="Invalid status"):
            VerificationResult(
                status="maybe",
                confidence=0.5,
                explanation="",
                raw_response="",
            )

    def test_confidence_clamped_high(self):
        r = VerificationResult(
            status="verified",
            confidence=1.5,
            explanation="",
            raw_response="",
        )
        assert r.confidence == 1.0

    def test_confidence_clamped_low(self):
        r = VerificationResult(
            status="verified",
            confidence=-0.3,
            explanation="",
            raw_response="",
        )
        assert r.confidence == 0.0

    def test_effectively_verified_for_verified(self):
        r = VerificationResult(
            status="verified",
            confidence=0.95,
            explanation="OK",
            raw_response="",
        )
        assert r.effectively_verified is True

    def test_effectively_verified_for_partially_verified(self):
        r = VerificationResult(
            status="partially_verified",
            confidence=0.8,
            explanation="Minor deviation",
            raw_response="",
        )
        assert r.effectively_verified is True

    def test_effectively_verified_false_for_not_verified(self):
        r = VerificationResult(
            status="not_verified",
            confidence=0.9,
            explanation="Missing",
            raw_response="",
        )
        assert r.effectively_verified is False

    def test_effectively_verified_false_for_unclear(self):
        r = VerificationResult(
            status="unclear",
            confidence=0.0,
            explanation="Cannot determine",
            raw_response="",
        )
        assert r.effectively_verified is False


# ---------------------------------------------------------------------------
# _parse_verification_result
# ---------------------------------------------------------------------------


class TestParseVerificationResult:
    """Tests for the internal JSON response parser."""

    def test_valid_json(self):
        raw = json.dumps({
            "status": "verified",
            "confidence": 0.92,
            "explanation": "The header 'Year' is visible in A1.",
        })
        result = _parse_verification_result(raw)
        assert result.status == "verified"
        assert result.confidence == pytest.approx(0.92)
        assert "Year" in result.explanation
        assert result.raw_response == raw

    def test_json_in_code_fence(self):
        raw = (
            "Here is my analysis:\n"
            "```json\n"
            '{"status": "not_verified", "confidence": 0.6, '
            '"explanation": "The cell is empty."}\n'
            "```\n"
            "That is my answer."
        )
        result = _parse_verification_result(raw)
        assert result.status == "not_verified"
        assert result.confidence == pytest.approx(0.6)

    def test_malformed_json_falls_back(self):
        raw = "I cannot parse this {broken json"
        result = _parse_verification_result(raw)
        assert result.status == "unclear"
        assert result.confidence == 0.0
        assert "Failed to parse" in result.explanation

    def test_invalid_status_falls_back(self):
        raw = json.dumps({
            "status": "probably",
            "confidence": 0.5,
            "explanation": "Not sure",
        })
        result = _parse_verification_result(raw)
        assert result.status == "unclear"

    def test_missing_confidence_defaults_zero(self):
        raw = json.dumps({
            "status": "verified",
            "explanation": "Looks good",
        })
        result = _parse_verification_result(raw)
        assert result.status == "verified"
        assert result.confidence == 0.0

    def test_non_dict_json_falls_back(self):
        raw = json.dumps(["verified", 0.9, "looks good"])
        result = _parse_verification_result(raw)
        assert result.status == "unclear"
        assert result.confidence == 0.0

    def test_partially_verified_parses(self):
        raw = json.dumps({
            "status": "partially_verified",
            "confidence": 0.85,
            "explanation": "Text is present but cursor moved.",
        })
        result = _parse_verification_result(raw)
        assert result.status == "partially_verified"
        assert result.confidence == pytest.approx(0.85)
        assert result.effectively_verified is True


# ---------------------------------------------------------------------------
# verify_step
# ---------------------------------------------------------------------------


class TestVerifyStep:
    """Tests for verify_step with mocked vlm_call."""

    @patch("openadapt_evals.vlm.vlm_call")
    def test_verified_response(self, mock_vlm_call):
        mock_vlm_call.return_value = json.dumps({
            "status": "verified",
            "confidence": 0.95,
            "explanation": "The text 'Year' is visible in cell A1.",
        })

        result = verify_step(
            FAKE_SCREENSHOT,
            'The text "Year" should appear in cell A1.',
        )

        assert result.status == "verified"
        assert result.confidence == pytest.approx(0.95)
        assert "Year" in result.explanation
        mock_vlm_call.assert_called_once()

        # Check that the call used the right parameters
        call_kwargs = mock_vlm_call.call_args
        assert call_kwargs.kwargs["images"] == [FAKE_SCREENSHOT]
        assert call_kwargs.kwargs["model"] == "gpt-4.1-mini"
        assert call_kwargs.kwargs["provider"] == "openai"

    @patch("openadapt_evals.vlm.vlm_call")
    def test_not_verified_response(self, mock_vlm_call):
        mock_vlm_call.return_value = json.dumps({
            "status": "not_verified",
            "confidence": 0.85,
            "explanation": "Cell A1 is empty; no text is visible.",
        })

        result = verify_step(FAKE_SCREENSHOT, "Cell A1 should contain text.")
        assert result.status == "not_verified"
        assert result.confidence == pytest.approx(0.85)

    @patch("openadapt_evals.vlm.vlm_call")
    def test_partially_verified_response(self, mock_vlm_call):
        mock_vlm_call.return_value = json.dumps({
            "status": "partially_verified",
            "confidence": 0.82,
            "explanation": (
                "The text 'Year' is present in cell A1, but the cursor "
                "has moved to cell A2 instead of remaining on A1."
            ),
        })

        result = verify_step(
            FAKE_SCREENSHOT,
            'Cell A1 should contain "Year" and be the active cell.',
        )
        assert result.status == "partially_verified"
        assert result.effectively_verified is True
        assert result.confidence == pytest.approx(0.82)

    @patch("openadapt_evals.vlm.vlm_call")
    def test_malformed_vlm_response(self, mock_vlm_call):
        mock_vlm_call.return_value = "Sorry, I cannot process this image."

        result = verify_step(FAKE_SCREENSHOT, "Some expectation")
        assert result.status == "unclear"
        assert result.confidence == 0.0
        assert "Failed to parse" in result.explanation

    @patch("openadapt_evals.vlm.vlm_call")
    def test_vlm_exception_returns_unclear(self, mock_vlm_call):
        mock_vlm_call.side_effect = RuntimeError("API timeout")

        result = verify_step(FAKE_SCREENSHOT, "Some expectation")
        assert result.status == "unclear"
        assert result.confidence == 0.0
        assert "API timeout" in result.explanation
        assert result.raw_response == ""

    @patch("openadapt_evals.vlm.vlm_call")
    def test_custom_model_and_provider(self, mock_vlm_call):
        mock_vlm_call.return_value = json.dumps({
            "status": "verified",
            "confidence": 0.9,
            "explanation": "OK",
        })

        verify_step(
            FAKE_SCREENSHOT,
            "Test",
            model="claude-sonnet-4-20250514",
            provider="anthropic",
            timeout=60,
        )

        call_kwargs = mock_vlm_call.call_args.kwargs
        assert call_kwargs["model"] == "claude-sonnet-4-20250514"
        assert call_kwargs["provider"] == "anthropic"
        assert call_kwargs["timeout"] == 60

    @patch("openadapt_evals.vlm.vlm_call")
    def test_json_in_code_fence_response(self, mock_vlm_call):
        mock_vlm_call.return_value = (
            "Based on my analysis:\n"
            "```json\n"
            '{"status": "verified", "confidence": 0.88, '
            '"explanation": "Header is present."}\n'
            "```"
        )

        result = verify_step(FAKE_SCREENSHOT, "Header should be present.")
        assert result.status == "verified"
        assert result.confidence == pytest.approx(0.88)

    @patch("openadapt_evals.vlm.vlm_call")
    def test_prompt_contains_outcome_focused_guidance(self, mock_vlm_call):
        """Verify the prompt includes outcome-focused verification rules."""
        mock_vlm_call.return_value = json.dumps({
            "status": "verified",
            "confidence": 0.9,
            "explanation": "OK",
        })

        verify_step(FAKE_SCREENSHOT, "Test expectation")

        prompt_arg = mock_vlm_call.call_args.args[0]
        # The prompt should mention outcome focus and cursor tolerance
        assert "OBSERVABLE OUTCOMES" in prompt_arg
        assert "cursor position" in prompt_arg.lower() or "cursor" in prompt_arg.lower()
        assert "partially_verified" in prompt_arg


# ---------------------------------------------------------------------------
# verify_plan_progress
# ---------------------------------------------------------------------------


class TestVerifyPlanProgress:
    """Tests for verify_plan_progress with mocked vlm_call."""

    PLAN_STEPS = [
        "Create a new sheet",
        "Type 'Year' in A1",
        "Type 'CA changes' in B1",
        "Type 'FA changes' in C1",
        "Type 'OA changes' in D1",
    ]

    @patch("openadapt_evals.vlm.vlm_call")
    def test_normal_progress(self, mock_vlm_call):
        mock_vlm_call.return_value = json.dumps({
            "completed_steps": [0, 1, 2],
            "current_step": 3,
            "confidence": 0.9,
        })

        result = verify_plan_progress(
            FAKE_SCREENSHOT, self.PLAN_STEPS, current_step_idx=2
        )

        assert result["completed_steps"] == [0, 1, 2]
        assert result["current_step"] == 3
        assert result["confidence"] == pytest.approx(0.9)

    @patch("openadapt_evals.vlm.vlm_call")
    def test_all_steps_completed(self, mock_vlm_call):
        mock_vlm_call.return_value = json.dumps({
            "completed_steps": [0, 1, 2, 3, 4],
            "current_step": 5,
            "confidence": 0.95,
        })

        result = verify_plan_progress(
            FAKE_SCREENSHOT, self.PLAN_STEPS, current_step_idx=4
        )

        assert result["completed_steps"] == [0, 1, 2, 3, 4]
        # current_step == len(plan_steps) is allowed (means "done")
        assert result["current_step"] == 5
        assert result["confidence"] == pytest.approx(0.95)

    @patch("openadapt_evals.vlm.vlm_call")
    def test_malformed_response_returns_fallback(self, mock_vlm_call):
        mock_vlm_call.return_value = "I cannot determine the progress."

        result = verify_plan_progress(
            FAKE_SCREENSHOT, self.PLAN_STEPS, current_step_idx=2
        )

        # Fallback: completed_steps = range(current_step_idx)
        assert result["completed_steps"] == [0, 1]
        assert result["current_step"] == 2
        assert result["confidence"] == 0.0

    @patch("openadapt_evals.vlm.vlm_call")
    def test_vlm_exception_returns_fallback(self, mock_vlm_call):
        mock_vlm_call.side_effect = ConnectionError("Network down")

        result = verify_plan_progress(
            FAKE_SCREENSHOT, self.PLAN_STEPS, current_step_idx=3
        )

        assert result["completed_steps"] == [0, 1, 2]
        assert result["current_step"] == 3
        assert result["confidence"] == 0.0

    @patch("openadapt_evals.vlm.vlm_call")
    def test_invalid_step_indices_filtered(self, mock_vlm_call):
        mock_vlm_call.return_value = json.dumps({
            "completed_steps": [0, 1, 99, -1, "bad"],
            "current_step": 2,
            "confidence": 0.7,
        })

        result = verify_plan_progress(
            FAKE_SCREENSHOT, self.PLAN_STEPS, current_step_idx=2
        )

        # Only valid indices (0 and 1) should remain
        assert result["completed_steps"] == [0, 1]
        assert result["current_step"] == 2

    @patch("openadapt_evals.vlm.vlm_call")
    def test_invalid_current_step_uses_fallback(self, mock_vlm_call):
        mock_vlm_call.return_value = json.dumps({
            "completed_steps": [0, 1],
            "current_step": -5,
            "confidence": 0.6,
        })

        result = verify_plan_progress(
            FAKE_SCREENSHOT, self.PLAN_STEPS, current_step_idx=2
        )

        # Invalid current_step falls back to provided current_step_idx
        assert result["current_step"] == 2

    @patch("openadapt_evals.vlm.vlm_call")
    def test_custom_model_and_provider(self, mock_vlm_call):
        mock_vlm_call.return_value = json.dumps({
            "completed_steps": [0],
            "current_step": 1,
            "confidence": 0.8,
        })

        verify_plan_progress(
            FAKE_SCREENSHOT,
            self.PLAN_STEPS,
            current_step_idx=1,
            model="gpt-4o",
            provider="openai",
            timeout=45,
        )

        call_kwargs = mock_vlm_call.call_args.kwargs
        assert call_kwargs["model"] == "gpt-4o"
        assert call_kwargs["timeout"] == 45

    @patch("openadapt_evals.vlm.vlm_call")
    def test_prompt_contains_outcome_focused_guidance(self, mock_vlm_call):
        """Verify the plan progress prompt is outcome-focused."""
        mock_vlm_call.return_value = json.dumps({
            "completed_steps": [0],
            "current_step": 1,
            "confidence": 0.8,
        })

        verify_plan_progress(
            FAKE_SCREENSHOT, self.PLAN_STEPS, current_step_idx=1
        )

        prompt_arg = mock_vlm_call.call_args.args[0]
        assert "CORE INTENDED EFFECT" in prompt_arg
        assert "cursor" in prompt_arg.lower()


# ---------------------------------------------------------------------------
# verify_goal_completion
# ---------------------------------------------------------------------------


class TestVerifyGoalCompletion:
    """Tests for verify_goal_completion with mocked vlm_call."""

    GOAL = (
        "In a new sheet with 4 headers 'Year', 'CA changes', 'FA changes', "
        "and 'OA changes', calculate the annual changes."
    )

    @patch("openadapt_evals.vlm.vlm_call")
    def test_goal_verified(self, mock_vlm_call):
        mock_vlm_call.return_value = json.dumps({
            "status": "verified",
            "confidence": 0.92,
            "explanation": "All four headers and calculated values are visible.",
        })

        result = verify_goal_completion(FAKE_SCREENSHOT, self.GOAL)
        assert result.status == "verified"
        assert result.confidence == pytest.approx(0.92)
        assert "headers" in result.explanation

    @patch("openadapt_evals.vlm.vlm_call")
    def test_goal_not_verified(self, mock_vlm_call):
        mock_vlm_call.return_value = json.dumps({
            "status": "not_verified",
            "confidence": 0.88,
            "explanation": "Only headers are visible but no calculated values.",
        })

        result = verify_goal_completion(FAKE_SCREENSHOT, self.GOAL)
        assert result.status == "not_verified"

    @patch("openadapt_evals.vlm.vlm_call")
    def test_goal_partially_verified(self, mock_vlm_call):
        mock_vlm_call.return_value = json.dumps({
            "status": "partially_verified",
            "confidence": 0.85,
            "explanation": (
                "All headers and computed values are present, but the "
                "percentage formatting has not been applied to the values."
            ),
        })

        result = verify_goal_completion(FAKE_SCREENSHOT, self.GOAL)
        assert result.status == "partially_verified"
        assert result.effectively_verified is True

    @patch("openadapt_evals.vlm.vlm_call")
    def test_vlm_exception_returns_unclear(self, mock_vlm_call):
        mock_vlm_call.side_effect = TimeoutError("Request timed out")

        result = verify_goal_completion(FAKE_SCREENSHOT, self.GOAL)
        assert result.status == "unclear"
        assert result.confidence == 0.0
        assert "timed out" in result.explanation

    @patch("openadapt_evals.vlm.vlm_call")
    def test_malformed_response_returns_unclear(self, mock_vlm_call):
        mock_vlm_call.return_value = "The goal seems partially done."

        result = verify_goal_completion(FAKE_SCREENSHOT, self.GOAL)
        assert result.status == "unclear"
        assert result.confidence == 0.0

    @patch("openadapt_evals.vlm.vlm_call")
    def test_custom_model(self, mock_vlm_call):
        mock_vlm_call.return_value = json.dumps({
            "status": "verified",
            "confidence": 0.9,
            "explanation": "Done.",
        })

        verify_goal_completion(
            FAKE_SCREENSHOT,
            self.GOAL,
            model="claude-sonnet-4-20250514",
            provider="anthropic",
            timeout=60,
        )

        call_kwargs = mock_vlm_call.call_args.kwargs
        assert call_kwargs["model"] == "claude-sonnet-4-20250514"
        assert call_kwargs["provider"] == "anthropic"
        assert call_kwargs["timeout"] == 60

    @patch("openadapt_evals.vlm.vlm_call")
    def test_prompt_contains_goal_text(self, mock_vlm_call):
        mock_vlm_call.return_value = json.dumps({
            "status": "verified",
            "confidence": 0.9,
            "explanation": "Done.",
        })

        verify_goal_completion(FAKE_SCREENSHOT, self.GOAL)

        # The prompt (first positional arg) should contain the goal text
        prompt_arg = mock_vlm_call.call_args.args[0]
        assert self.GOAL in prompt_arg

    @patch("openadapt_evals.vlm.vlm_call")
    def test_prompt_contains_outcome_focused_guidance(self, mock_vlm_call):
        """Verify the goal prompt includes outcome-focused rules."""
        mock_vlm_call.return_value = json.dumps({
            "status": "verified",
            "confidence": 0.9,
            "explanation": "Done.",
        })

        verify_goal_completion(FAKE_SCREENSHOT, self.GOAL)

        prompt_arg = mock_vlm_call.call_args.args[0]
        assert "SUBSTANTIVE OUTCOME" in prompt_arg
        assert "partially_verified" in prompt_arg


# ---------------------------------------------------------------------------
# False-negative regression tests
# ---------------------------------------------------------------------------
# These tests simulate the specific false-negative scenarios from the Level 3
# live eval where the VLM verifier was too strict.


class TestFalseNegativeRegressions:
    """Tests for specific false-negative scenarios from live evaluation.

    These verify that the updated prompts and status model correctly handle
    cases where the old verifier would produce false negatives.

    Note: These tests validate the parsing pipeline, not the VLM's actual
    response to the new prompts. Live eval is needed to validate prompt
    effectiveness.
    """

    # -- Scenario 1: Header typed correctly, cursor moved after entry ------

    @patch("openadapt_evals.vlm.vlm_call")
    def test_header_entered_but_cursor_moved(self, mock_vlm_call):
        """Steps 4-5 regression: headers WERE entered correctly, but VLM
        said 'not_verified' because cursor was in a different cell.

        The updated prompt should guide the VLM to return 'verified' or
        'partially_verified' when the text IS present in the correct cell,
        regardless of where the cursor sits now.
        """
        # Simulate VLM correctly interpreting the updated prompt:
        # text is in the right cell, cursor moved -> partially_verified
        mock_vlm_call.return_value = json.dumps({
            "status": "partially_verified",
            "confidence": 0.88,
            "explanation": (
                "The header 'CA changes' is visible in cell B1, which is "
                "the correct location. However, the active cell indicator "
                "is on cell C1, not B1. Since the core outcome (text in the "
                "correct cell) is achieved, this is partially verified."
            ),
        })

        result = verify_step(
            FAKE_SCREENSHOT,
            "Cell B1 should contain the header 'CA changes' and be selected.",
        )

        assert result.status == "partially_verified"
        assert result.effectively_verified is True
        assert result.confidence > 0.7

    @patch("openadapt_evals.vlm.vlm_call")
    def test_header_entered_cursor_moved_still_verified(self, mock_vlm_call):
        """When the expectation only asks about content (not selection),
        the VLM should return 'verified' even if cursor is elsewhere."""
        mock_vlm_call.return_value = json.dumps({
            "status": "verified",
            "confidence": 0.93,
            "explanation": (
                "The header 'FA changes' is clearly visible in cell C1. "
                "The cursor position is irrelevant to this expectation."
            ),
        })

        result = verify_step(
            FAKE_SCREENSHOT,
            "Cell C1 should contain the header 'FA changes'.",
        )

        assert result.status == "verified"
        assert result.effectively_verified is True

    # -- Scenario 2: Correct numeric value, semantic dispute ---------------

    @patch("openadapt_evals.vlm.vlm_call")
    def test_correct_numeric_value_semantic_dispute(self, mock_vlm_call):
        """Step 11 regression: cell D2 had the correct value (-0.0167598)
        but VLM disputed whether it represented '2015 to 2016' change vs
        '2015 itself'. This is a semantic label dispute, not an actual error.

        The updated prompt instructs the VLM to verify the VALUE IS CORRECT,
        not to dispute the semantic interpretation of what the value
        represents.
        """
        mock_vlm_call.return_value = json.dumps({
            "status": "verified",
            "confidence": 0.90,
            "explanation": (
                "Cell D2 contains the value -0.0167598 which matches the "
                "expected numeric value. The semantic question of whether "
                "this represents '2015 to 2016' change or '2015 itself' is "
                "beyond what can be verified from the screenshot; the "
                "numeric value is correct."
            ),
        })

        result = verify_step(
            FAKE_SCREENSHOT,
            "Cell D2 should contain the OA year-over-year change value "
            "(-0.0167598) for 2015-2016.",
        )

        assert result.status == "verified"
        assert result.effectively_verified is True

    @patch("openadapt_evals.vlm.vlm_call")
    def test_correct_value_rounding_difference(self, mock_vlm_call):
        """Numeric value is correct but displayed with different decimal
        places. Should still verify."""
        mock_vlm_call.return_value = json.dumps({
            "status": "verified",
            "confidence": 0.88,
            "explanation": (
                "Cell D2 shows -0.017 which is -0.0167598 rounded to "
                "3 decimal places. The value is correct within reasonable "
                "rounding."
            ),
        })

        result = verify_step(
            FAKE_SCREENSHOT,
            "Cell D2 should contain approximately -0.0167598.",
        )

        assert result.status == "verified"
        assert result.effectively_verified is True

    # -- Scenario 3: Formatting not applied (real failure) -----------------

    @patch("openadapt_evals.vlm.vlm_call")
    def test_formatting_not_applied_is_not_verified(self, mock_vlm_call):
        """Step 13 regression: the agent sent wrong keystroke (Ctrl+S
        instead of %) so percentage formatting was NOT applied. This IS a
        real failure and should be 'not_verified'.

        The prompt instructs the VLM to check whether the VISUAL FORMAT
        actually changed, which it did not in this case.
        """
        mock_vlm_call.return_value = json.dumps({
            "status": "not_verified",
            "confidence": 0.92,
            "explanation": (
                "The cells still display raw decimal values (e.g., "
                "-0.0167598) instead of percentage format (e.g., -1.68%). "
                "The formatting action did not have its intended effect."
            ),
        })

        result = verify_step(
            FAKE_SCREENSHOT,
            "The values in column D should be formatted as percentages.",
        )

        assert result.status == "not_verified"
        assert result.effectively_verified is False

    @patch("openadapt_evals.vlm.vlm_call")
    def test_formatting_partially_applied(self, mock_vlm_call):
        """Values are correct and formatting partially applied (e.g., some
        cells formatted, others not). Should be partially_verified."""
        mock_vlm_call.return_value = json.dumps({
            "status": "partially_verified",
            "confidence": 0.78,
            "explanation": (
                "Some cells in column D show percentage format (D2: -1.68%) "
                "but others still show decimal format (D5: 0.0234). The "
                "formatting was partially applied."
            ),
        })

        result = verify_step(
            FAKE_SCREENSHOT,
            "All values in column D should be formatted as percentages.",
        )

        assert result.status == "partially_verified"
        assert result.effectively_verified is True

    # -- Scenario 4: Action had no effect (true negative, should stay) -----

    @patch("openadapt_evals.vlm.vlm_call")
    def test_action_had_no_effect_stays_not_verified(self, mock_vlm_call):
        """When the action truly had no observable effect, the VLM should
        still return 'not_verified'. This is NOT a false negative."""
        mock_vlm_call.return_value = json.dumps({
            "status": "not_verified",
            "confidence": 0.95,
            "explanation": (
                "Cell A1 is completely empty. No text was entered. The "
                "action had no observable effect."
            ),
        })

        result = verify_step(
            FAKE_SCREENSHOT,
            "Cell A1 should contain the text 'Year'.",
        )

        assert result.status == "not_verified"
        assert result.effectively_verified is False

    # -- Scenario 5: Multiple headers all present, plan progress -----------

    @patch("openadapt_evals.vlm.vlm_call")
    def test_plan_progress_credits_completed_headers(self, mock_vlm_call):
        """When all headers are visible in their correct cells, plan
        progress should credit those steps as completed regardless of
        current cursor position."""
        mock_vlm_call.return_value = json.dumps({
            "completed_steps": [0, 1, 2, 3, 4],
            "current_step": 5,
            "confidence": 0.90,
        })

        plan_steps = [
            "Create a new sheet",
            "Type 'Year' in A1",
            "Type 'CA changes' in B1",
            "Type 'FA changes' in C1",
            "Type 'OA changes' in D1",
        ]

        result = verify_plan_progress(
            FAKE_SCREENSHOT, plan_steps, current_step_idx=3
        )

        # All 5 steps should be credited as complete
        assert result["completed_steps"] == [0, 1, 2, 3, 4]
        assert result["current_step"] == 5

    # -- Scenario 6: Goal with correct values but missing formatting -------

    @patch("openadapt_evals.vlm.vlm_call")
    def test_goal_values_correct_formatting_missing(self, mock_vlm_call):
        """Goal check where all computed values are correct but percentage
        formatting was not applied. Should be 'partially_verified' since
        the substantive computation is done."""
        mock_vlm_call.return_value = json.dumps({
            "status": "partially_verified",
            "confidence": 0.82,
            "explanation": (
                "All four headers (Year, CA changes, FA changes, OA changes) "
                "are present and all computed year-over-year change values "
                "are correct. However, the values are displayed as raw "
                "decimals rather than percentages. The goal is substantially "
                "achieved with a minor formatting gap."
            ),
        })

        goal = (
            "Calculate annual changes in CA, FA, and OA, display them in "
            "a new sheet with headers, and format values as percentages."
        )

        result = verify_goal_completion(FAKE_SCREENSHOT, goal)
        assert result.status == "partially_verified"
        assert result.effectively_verified is True
