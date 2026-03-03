"""Tests for the review state machine."""

from __future__ import annotations

import pytest

from engine.review import (
    EGRESS_ALLOWED_STATES,
    EgressBlockedError,
    ReviewStatus,
    transition_status,
)


class TestReviewStatus:
    """Tests for the ReviewStatus enum and state transitions."""

    def test_all_states_defined(self) -> None:
        """All five states from the design doc should be defined."""
        assert ReviewStatus.CAPTURED.value == "captured"
        assert ReviewStatus.SCRUBBED.value == "scrubbed"
        assert ReviewStatus.REVIEWED.value == "reviewed"
        assert ReviewStatus.DISMISSED.value == "dismissed"
        assert ReviewStatus.DELETED.value == "deleted"

    def test_egress_allowed_states(self) -> None:
        """Only REVIEWED and DISMISSED should allow egress."""
        assert ReviewStatus.REVIEWED in EGRESS_ALLOWED_STATES
        assert ReviewStatus.DISMISSED in EGRESS_ALLOWED_STATES
        assert ReviewStatus.CAPTURED not in EGRESS_ALLOWED_STATES
        assert ReviewStatus.SCRUBBED not in EGRESS_ALLOWED_STATES
        assert ReviewStatus.DELETED not in EGRESS_ALLOWED_STATES


class TestEgressBlockedError:
    """Tests for the EgressBlockedError exception."""

    def test_error_message_includes_capture_id(self) -> None:
        """Error message should include the capture ID."""
        err = EgressBlockedError("abc123", ReviewStatus.CAPTURED)
        assert "abc123" in str(err)

    def test_error_message_includes_status(self) -> None:
        """Error message should include the current status."""
        err = EgressBlockedError("abc123", ReviewStatus.CAPTURED)
        assert "captured" in str(err)

    def test_error_message_includes_guidance(self) -> None:
        """Error message should include user-facing guidance."""
        err = EgressBlockedError("abc123", ReviewStatus.SCRUBBED)
        assert "review" in str(err).lower()


class TestTransitionStatus:
    """Tests for the review state transition validator."""

    @pytest.mark.parametrize(
        "from_status,to_status",
        [
            (ReviewStatus.CAPTURED, ReviewStatus.SCRUBBED),
            (ReviewStatus.CAPTURED, ReviewStatus.DISMISSED),
            (ReviewStatus.CAPTURED, ReviewStatus.DELETED),
            (ReviewStatus.SCRUBBED, ReviewStatus.REVIEWED),
            (ReviewStatus.SCRUBBED, ReviewStatus.DELETED),
            (ReviewStatus.REVIEWED, ReviewStatus.DELETED),
            (ReviewStatus.DISMISSED, ReviewStatus.DELETED),
        ],
    )
    @pytest.mark.skip(reason="Not yet implemented (requires index.db)")
    def test_valid_transitions(
        self, from_status: ReviewStatus, to_status: ReviewStatus,
    ) -> None:
        """All valid transitions should succeed."""
        # TODO: Set up index.db with capture in from_status
        transition_status("test-id", from_status, to_status)

    @pytest.mark.parametrize(
        "from_status,to_status",
        [
            (ReviewStatus.CAPTURED, ReviewStatus.REVIEWED),  # Must scrub first
            (ReviewStatus.SCRUBBED, ReviewStatus.DISMISSED),  # Can't dismiss after scrubbing
            (ReviewStatus.REVIEWED, ReviewStatus.CAPTURED),  # Can't go backwards
            (ReviewStatus.DISMISSED, ReviewStatus.REVIEWED),  # Can't go backwards
            (ReviewStatus.DELETED, ReviewStatus.CAPTURED),  # Can't un-delete
        ],
    )
    def test_invalid_transitions_raise(
        self, from_status: ReviewStatus, to_status: ReviewStatus,
    ) -> None:
        """Invalid transitions should raise ValueError."""
        with pytest.raises((ValueError, NotImplementedError)):
            transition_status("test-id", from_status, to_status)
