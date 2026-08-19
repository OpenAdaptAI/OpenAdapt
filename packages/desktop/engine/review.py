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
               v
         +-------------------------+
         |   CLEARED FOR EGRESS   |  <- Only the reviewed scrubbed copy
         +-------------------------+

``DISMISSED`` records a local choice only. It never permits raw-data egress.

All outbound data paths MUST call check_egress_allowed() before sending
any data off-machine. This is the single enforcement point.
"""

from __future__ import annotations

import enum
import hashlib
import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from engine.audit import AuditLogger
    from engine.db import IndexDB


class ReviewStatus(enum.Enum):
    """Review state for a capture session.

    Attributes:
        CAPTURED:  Raw recording just created. Pending review. Blocked from all egress.
        SCRUBBED:  Scrub pass completed, awaiting user review. Still blocked.
        REVIEWED:  User reviewed scrubbed copy and approved. Scrubbed copy cleared for egress.
        DISMISSED: User skipped scrubbing. Raw data remains local and blocked from egress.
        DELETED:   Recording deleted from disk.
    """

    CAPTURED = "captured"
    SCRUBBED = "scrubbed"
    REVIEWED = "reviewed"
    DISMISSED = "dismissed"
    DELETED = "deleted"


# Only a reviewed sanitized derivative can leave the machine.  `DISMISSED`
# remains a persisted legacy/local-review state, but it never grants egress.
EGRESS_ALLOWED_STATES = frozenset({ReviewStatus.REVIEWED})

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


class EgressArtifactError(Exception):
    """Raised when the approved sanitized derivative is absent or unsafe."""


_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


def load_derivative_approval(path: Path) -> str:
    """Return the exact approved tree digest from a closed local schema."""

    try:
        review = json.loads((path / "review_status.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EgressArtifactError("The sanitized derivative has no valid approval.") from exc
    if not isinstance(review, dict) or set(review) != {"status", "approved_tree_sha256"}:
        raise EgressArtifactError("The sanitized derivative approval schema is invalid.")
    approved_digest = review.get("approved_tree_sha256")
    if review.get("status") != "reviewed" or not isinstance(
        approved_digest, str
    ) or not _SHA256_RE.fullmatch(approved_digest):
        raise EgressArtifactError("The sanitized derivative has no exact approval.")
    return approved_digest


def _stream_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def update_derivative_tree_digest(
    digest: Any,
    *,
    relative_path: str,
    member_type: str,
    file_sha256: str | None = None,
) -> None:
    """Add one unambiguous, type-aware record to a derivative tree hash."""

    path_bytes = relative_path.encode("utf-8")
    if member_type not in {"directory", "file"}:
        raise EgressArtifactError("The sanitized derivative has an unsupported member type.")
    digest.update(b"openadapt.desktop-derivative-member/v1\0")
    digest.update(b"D" if member_type == "directory" else b"F")
    digest.update(len(path_bytes).to_bytes(8, "big"))
    digest.update(path_bytes)
    if member_type == "file":
        if not isinstance(file_sha256, str) or not _SHA256_RE.fullmatch(file_sha256):
            raise EgressArtifactError("The sanitized derivative file digest is invalid.")
        digest.update(bytes.fromhex(file_sha256))


def derivative_tree_sha256(path: Path) -> str:
    """Hash a derivative tree without loading recording media into memory."""

    digest = hashlib.sha256()
    members = [path, *sorted(path.rglob("*"))] if path.is_dir() else [path]
    for member in members:
        if member.is_symlink():
            raise EgressArtifactError("The sanitized derivative contains a symlink.")
        relative = "." if member == path else member.relative_to(path).as_posix()
        if relative == "review_status.json":
            continue
        if member.is_file():
            stat = os.stat(member, follow_symlinks=False)
            if stat.st_nlink != 1:
                raise EgressArtifactError(
                    "The sanitized derivative contains a hard-linked file."
                )
            update_derivative_tree_digest(
                digest,
                relative_path=relative,
                member_type="file",
                file_sha256=_stream_sha256(member),
            )
        elif member.is_dir():
            update_derivative_tree_digest(
                digest,
                relative_path=relative,
                member_type="directory",
            )
        else:
            raise EgressArtifactError(
                "The sanitized derivative has an unsupported member type."
            )
    return digest.hexdigest()


def approved_egress_path(capture_id: str, db: IndexDB) -> Path:
    """Return the exact reviewed derivative that an upload worker may send.

    The database status is not sufficient. This function also proves that the
    selected path is the distinct scrubbed copy and contains no symlinks. The
    upload worker calls it again immediately before network egress.
    """

    capture = db.get_capture(capture_id)
    if capture is None:
        raise ValueError(f"Unknown capture: {capture_id}")
    status = ReviewStatus(capture["review_status"])
    if status not in EGRESS_ALLOWED_STATES:
        raise EgressBlockedError(capture_id, status)

    raw_value = str(capture.get("capture_path") or "").strip()
    scrubbed_value = str(capture.get("scrubbed_path") or "").strip()
    if not scrubbed_value:
        raise EgressArtifactError(f"Recording '{capture_id}' has no approved sanitized derivative.")
    scrubbed_candidate = Path(scrubbed_value)
    if scrubbed_candidate.is_symlink():
        raise EgressArtifactError(
            f"Recording '{capture_id}' has a symlink as its sanitized derivative."
        )
    try:
        scrubbed = scrubbed_candidate.resolve(strict=True)
        raw = Path(raw_value).resolve(strict=True)
    except OSError as exc:
        raise EgressArtifactError(
            f"Recording '{capture_id}' has an unavailable sanitized derivative."
        ) from exc
    if scrubbed == raw:
        raise EgressArtifactError(
            f"Recording '{capture_id}' points its sanitized derivative at raw data."
        )
    expected = raw.parent / f"{raw.name}.scrubbed"
    if scrubbed != expected:
        raise EgressArtifactError(
            f"Recording '{capture_id}' does not use its canonical sanitized derivative."
        )
    paths = [scrubbed, *scrubbed.rglob("*")] if scrubbed.is_dir() else [scrubbed]
    if any(path.is_symlink() for path in paths):
        raise EgressArtifactError(
            f"Recording '{capture_id}' has a symlink in its sanitized derivative."
        )
    linked_file = next(
        (
            path
            for path in paths
            if path.is_file() and os.stat(path, follow_symlinks=False).st_nlink != 1
        ),
        None,
    )
    if linked_file is not None:
        raise EgressArtifactError(
            f"Recording '{capture_id}' has a hard-linked file in its sanitized derivative."
        )
    if not scrubbed.is_dir():
        raise EgressArtifactError(
            f"Recording '{capture_id}' has no approved sanitized derivative directory."
        )
    documents: dict[str, dict] = {}
    for name in ("scrub_manifest.json", "review_status.json"):
        manifest_path = scrubbed / name
        try:
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EgressArtifactError(
                f"Recording '{capture_id}' has no valid {name}."
            ) from exc
        if not isinstance(value, dict):
            raise EgressArtifactError(
                f"Recording '{capture_id}' has no valid {name}."
            )
        documents[name] = value
    manifest = documents["scrub_manifest.json"]
    if manifest.get("scrub_level") == "basic" and any(
        path.is_file() for path in (scrubbed / "screenshots").rglob("*")
    ):
        raise EgressArtifactError(
            f"Recording '{capture_id}' used basic scrubbing for screenshots. "
            "Image-capable scrubbing is required before egress."
        )
    approved_digest = load_derivative_approval(scrubbed)
    if derivative_tree_sha256(scrubbed) != approved_digest:
        raise EgressArtifactError(
            f"Recording '{capture_id}' changed after its local review."
        )
    return scrubbed


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
        True if the capture has a reviewed sanitized derivative.

    Raises:
        EgressBlockedError: If the capture is not in reviewed state.
        EgressArtifactError: If the reviewed derivative is absent or unsafe.
        ValueError: If the capture does not exist.
    """
    approved_egress_path(capture_id, db)
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
        if from_status == ReviewStatus.SCRUBBED and to_status == ReviewStatus.REVIEWED:
            scrubbed_value = str(capture.get("scrubbed_path") or "").strip()
            if not scrubbed_value:
                raise EgressArtifactError(
                    f"Recording '{capture_id}' has no sanitized derivative to approve."
                )
            scrubbed = Path(scrubbed_value)
            expected = Path(capture["capture_path"]).parent / (
                Path(capture["capture_path"]).name + ".scrubbed"
            )
            if scrubbed.is_symlink() or scrubbed.resolve(strict=True) != expected.resolve():
                raise EgressArtifactError(
                    f"Recording '{capture_id}' does not use its canonical sanitized derivative."
                )
            manifest_path = scrubbed / "scrub_manifest.json"
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise EgressArtifactError(
                    f"Recording '{capture_id}' has no valid scrub_manifest.json."
                ) from exc
            if not isinstance(manifest, dict):
                raise EgressArtifactError(
                    f"Recording '{capture_id}' has no valid scrub_manifest.json."
                )
            approval_path = scrubbed / "review_status.json"
            temporary = scrubbed / ".review_status.json.tmp"
            approval = {
                "status": "reviewed",
                "approved_tree_sha256": derivative_tree_sha256(scrubbed),
            }
            temporary.write_text(
                json.dumps(approval, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            os.chmod(temporary, 0o600)
            temporary.replace(approval_path)
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
