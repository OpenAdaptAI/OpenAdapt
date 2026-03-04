"""Tests for the review state machine."""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.db import IndexDB
from engine.review import (
    EGRESS_ALLOWED_STATES,
    EgressBlockedError,
    ReviewStatus,
    check_egress_allowed,
    get_pending_reviews,
    transition_status,
)


@pytest.fixture
def db(tmp_path: Path) -> IndexDB:
    """Create a temporary index database."""
    d = IndexDB(tmp_path / "index.db")
    d.initialize()
    yield d
    d.close()


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
    def test_valid_transitions(
        self, db: IndexDB, from_status: ReviewStatus, to_status: ReviewStatus,
    ) -> None:
        """All valid transitions should succeed."""
        db.insert_capture("test-id", "/tmp/cap", "2026-03-02T10:00:00Z")
        db.update_capture("test-id", review_status=from_status.value)
        transition_status("test-id", from_status, to_status, db=db)
        cap = db.get_capture("test-id")
        assert cap["review_status"] == to_status.value

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
        with pytest.raises(ValueError):
            transition_status("test-id", from_status, to_status)


class TestCheckEgress:
    """Tests for the egress check function."""

    def test_egress_allowed_reviewed(self, db: IndexDB) -> None:
        """Reviewed captures should be allowed for egress."""
        db.insert_capture("test-id", "/tmp/cap", "2026-03-02T10:00:00Z")
        db.update_capture("test-id", review_status="reviewed")
        assert check_egress_allowed("test-id", db) is True

    def test_egress_allowed_dismissed(self, db: IndexDB) -> None:
        """Dismissed captures should be allowed for egress."""
        db.insert_capture("test-id", "/tmp/cap", "2026-03-02T10:00:00Z")
        db.update_capture("test-id", review_status="dismissed")
        assert check_egress_allowed("test-id", db) is True

    def test_egress_blocked_captured(self, db: IndexDB) -> None:
        """Captured captures should be blocked from egress."""
        db.insert_capture("test-id", "/tmp/cap", "2026-03-02T10:00:00Z")
        with pytest.raises(EgressBlockedError):
            check_egress_allowed("test-id", db)

    def test_egress_blocked_scrubbed(self, db: IndexDB) -> None:
        """Scrubbed captures should be blocked from egress."""
        db.insert_capture("test-id", "/tmp/cap", "2026-03-02T10:00:00Z")
        db.update_capture("test-id", review_status="scrubbed")
        with pytest.raises(EgressBlockedError):
            check_egress_allowed("test-id", db)

    def test_egress_unknown_capture(self, db: IndexDB) -> None:
        """Unknown capture should raise ValueError."""
        with pytest.raises(ValueError, match="Unknown"):
            check_egress_allowed("nonexistent", db)


class TestGetPendingReviews:
    """Tests for the pending reviews query."""

    def test_returns_captured_and_scrubbed(self, db: IndexDB) -> None:
        """Should return only captured and scrubbed captures."""
        db.insert_capture("a", "/tmp/a", "2026-03-01T10:00:00Z")
        db.insert_capture("b", "/tmp/b", "2026-03-02T10:00:00Z")
        db.insert_capture("c", "/tmp/c", "2026-03-03T10:00:00Z")
        db.update_capture("b", review_status="scrubbed")
        db.update_capture("c", review_status="reviewed")

        pending = get_pending_reviews(db)
        ids = {c["capture_id"] for c in pending}
        assert ids == {"a", "b"}
