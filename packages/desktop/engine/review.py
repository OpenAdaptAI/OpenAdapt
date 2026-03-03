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


def check_egress_allowed(capture_id: str) -> bool:
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

    Returns:
        True if the capture is cleared for egress.

    Raises:
        EgressBlockedError: If the capture is in captured or scrubbed state.
    """
    # TODO: Look up review status from index.db
    # status = _get_review_status(capture_id)
    # if status not in EGRESS_ALLOWED_STATES:
    #     raise EgressBlockedError(capture_id, status)
    # return True
    raise NotImplementedError


def transition_status(
    capture_id: str,
    from_status: ReviewStatus,
    to_status: ReviewStatus,
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

    Raises:
        ValueError: If the transition is not allowed.
    """
    valid_transitions: dict[ReviewStatus, frozenset[ReviewStatus]] = {
        ReviewStatus.CAPTURED: frozenset(
            {ReviewStatus.SCRUBBED, ReviewStatus.DISMISSED, ReviewStatus.DELETED}
        ),
        ReviewStatus.SCRUBBED: frozenset({ReviewStatus.REVIEWED, ReviewStatus.DELETED}),
        ReviewStatus.REVIEWED: frozenset({ReviewStatus.DELETED}),
        ReviewStatus.DISMISSED: frozenset({ReviewStatus.DELETED}),
    }

    allowed = valid_transitions.get(from_status, frozenset())
    if to_status not in allowed:
        raise ValueError(
            f"Invalid transition: {from_status.value} -> {to_status.value}. "
            f"Allowed from {from_status.value}: "
            f"{', '.join(s.value for s in allowed) or 'none'}"
        )

    # TODO: Update review status in index.db
    # TODO: Log transition to audit.jsonl
    raise NotImplementedError


def get_pending_reviews() -> list[dict]:
    """Get all captures that are pending review.

    Returns captures in `captured` or `scrubbed` state.

    Returns:
        List of capture metadata dicts with review status.
    """
    # TODO: Query index.db for captures in captured/scrubbed state
    raise NotImplementedError
