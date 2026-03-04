"""Upload review state machine and egress gating.

Every recording has a review status that persists in the index database.
This status gates ALL outbound data paths -- not just storage uploads, but
also VLM API calls, annotation pipelines, federated learning, sharing,
and any future feature that sends data off-machine.

State machine (from design doc Section 5):

                      +-------------+
                      |  CAPTURED   |  <- Initial state. Raw on disk.
                      |  (pending)  |     NOTHING can send this data
                      +------+------+     off-machine.
                             |
                +------------+------------+
                |            |            |
                v            v            v
         +----------+  +-----------+  +----------+
         | SCRUBBED  |  | DISMISSED |  | DELETED  |
         | (pending  |  | (user     |  |          |
         |  review)  |  |  accepted |  +----------+
         +-----+-----+  |  risks)   |
               |         +-----+-----+
               v               |
         +----------+          |
         | REVIEWED  |         |
         | (approved |         |
         |  scrubbed |         |
         |  copy)    |         |
         +-----+-----+         |
               |               |
               v               v
         +-------------------------+
         |   CLEARED FOR EGRESS   |  <- Data can now be sent to:
         |                        |    storage backends, VLM APIs,
         |                        |    annotation pipelines, FL, etc.
         +-------------------------+

All outbound data paths MUST call check_egress_allowed() before sending
any data off-machine. This is the single enforcement point.
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.audit import AuditLogger
    from engine.db import IndexDB


class ReviewStatus(enum.Enum):
    """Review state for a capture session.

    Attributes:
        CAPTURED:  Raw recording just created. Pending review. Blocked from all egress.
        SCRUBBED:  Scrub pass completed, awaiting user review. Still blocked.
        REVIEWED:  User reviewed scrubbed copy and approved. Scrubbed copy cleared for egress.
        DISMISSED: User skipped scrubbing, accepted PII risks. Raw data cleared for egress.
        DELETED:   Recording deleted from disk.
    """

    CAPTURED = "captured"
    SCRUBBED = "scrubbed"
    REVIEWED = "reviewed"
    DISMISSED = "dismissed"
    DELETED = "deleted"


# States that allow data to leave the machine.
EGRESS_ALLOWED_STATES = frozenset({ReviewStatus.REVIEWED, ReviewStatus.DISMISSED})

# Valid state transitions.
VALID_TRANSITIONS: dict[ReviewStatus, frozenset[ReviewStatus]] = {
    ReviewStatus.CAPTURED: frozenset(
        {ReviewStatus.SCRUBBED, ReviewStatus.DISMISSED, ReviewStatus.DELETED}
    ),
    ReviewStatus.SCRUBBED: frozenset({ReviewStatus.REVIEWED, ReviewStatus.DELETED}),
    ReviewStatus.REVIEWED: frozenset({ReviewStatus.DELETED}),
    ReviewStatus.DISMISSED: frozenset({ReviewStatus.DELETED}),
}


class EgressBlockedError(Exception):
    """Raised when an outbound data path is attempted on an unreviewed capture.

    This error includes a user-facing message suitable for display in the UI.
    """

    def __init__(self, capture_id: str, current_status: ReviewStatus) -> None:
        self.capture_id = capture_id
        self.current_status = current_status
        super().__init__(
            f"Recording '{capture_id}' hasn't been reviewed yet "
            f"(status: {current_status.value}). "
            "Open the review panel to approve it for sharing."
        )


def check_egress_allowed(capture_id: str, db: IndexDB) -> bool:
    """Check whether a capture is cleared for egress.

    This is the single enforcement point that ALL outbound data paths must call
    before sending any recording data off-machine. This includes:
      - Storage backend uploads (S3, R2, HF Hub, MinIO, Wormhole)
      - VLM API calls (OpenAI Vision, Anthropic Claude, Google Gemini)
      - Annotation pipelines
      - Federated learning gradient computation
      - Any future egress path

    Args:
        capture_id: The capture session ID to check.
        db: The index database instance.

    Returns:
        True if the capture is cleared for egress.

    Raises:
        EgressBlockedError: If the capture is in captured or scrubbed state.
        ValueError: If the capture does not exist.
    """
    capture = db.get_capture(capture_id)
    if capture is None:
        raise ValueError(f"Unknown capture: {capture_id}")
    status = ReviewStatus(capture["review_status"])
    if status not in EGRESS_ALLOWED_STATES:
        raise EgressBlockedError(capture_id, status)
    return True


def transition_status(
    capture_id: str,
    from_status: ReviewStatus,
    to_status: ReviewStatus,
    db: IndexDB | None = None,
    audit: AuditLogger | None = None,
) -> None:
    """Transition a capture's review status.

    Validates the transition is legal according to the state machine.

    Valid transitions:
        captured  -> scrubbed, dismissed, deleted
        scrubbed  -> reviewed, deleted
        reviewed  -> deleted
        dismissed -> deleted

    Args:
        capture_id: The capture session ID.
        from_status: Expected current status.
        to_status: Target status.
        db: The index database instance. Required for persistence.
        audit: Optional audit logger for transition logging.

    Raises:
        ValueError: If the transition is not allowed or current status doesn't match.
    """
    allowed = VALID_TRANSITIONS.get(from_status, frozenset())
    if to_status not in allowed:
        raise ValueError(
            f"Invalid transition: {from_status.value} -> {to_status.value}. "
            f"Allowed from {from_status.value}: "
            f"{', '.join(s.value for s in allowed) or 'none'}"
        )

    if db is not None:
        capture = db.get_capture(capture_id)
        if capture is None:
            raise ValueError(f"Unknown capture: {capture_id}")
        current = ReviewStatus(capture["review_status"])
        if current != from_status:
            raise ValueError(
                f"Status mismatch for '{capture_id}': "
                f"expected {from_status.value}, got {current.value}"
            )
        db.update_capture(capture_id, review_status=to_status.value)

    if audit is not None:
        audit.log(
            "review_transition",
            capture_id=capture_id,
            from_status=from_status.value,
            to_status=to_status.value,
        )


def get_pending_reviews(db: IndexDB) -> list[dict]:
    """Get all captures that are pending review.

    Returns captures in `captured` or `scrubbed` state.

    Args:
        db: The index database instance.

    Returns:
        List of capture metadata dicts with review status.
    """
    return db.get_pending_reviews()
