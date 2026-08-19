"""Tests for the review state machine."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from engine.db import IndexDB
from engine.review import (
    EGRESS_ALLOWED_STATES,
    EgressArtifactError,
    EgressBlockedError,
    ReviewStatus,
    approved_egress_path,
    check_egress_allowed,
    get_pending_reviews,
    transition_status,
)


def test_derivative_tree_digest_distinguishes_member_type_and_path(tmp_path: Path) -> None:
    """A directory name cannot collide with a file path plus its content hash."""

    from engine.review import derivative_tree_sha256

    derivative = tmp_path / "capture.scrubbed"
    derivative.mkdir()
    secret = b"secret bytes that must not pass an old approval"
    collision_name = "a" + hashlib.sha256(secret).hexdigest()
    directory = derivative / collision_name
    directory.mkdir()
    directory_digest = derivative_tree_sha256(derivative)

    directory.rmdir()
    (derivative / "a").write_bytes(secret)
    file_digest = derivative_tree_sha256(derivative)

    assert file_digest != directory_digest


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
        """Only REVIEWED should be eligible for egress."""
        assert ReviewStatus.REVIEWED in EGRESS_ALLOWED_STATES
        assert ReviewStatus.DISMISSED not in EGRESS_ALLOWED_STATES
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
        self,
        db: IndexDB,
        tmp_path: Path,
        from_status: ReviewStatus,
        to_status: ReviewStatus,
    ) -> None:
        """All valid transitions should succeed."""
        raw = tmp_path / "cap"
        raw.mkdir()
        db.insert_capture("test-id", str(raw), "2026-03-02T10:00:00Z")
        db.update_capture("test-id", review_status=from_status.value)
        if from_status == ReviewStatus.SCRUBBED and to_status == ReviewStatus.REVIEWED:
            scrubbed = tmp_path / "cap.scrubbed"
            scrubbed.mkdir()
            (scrubbed / "scrub_manifest.json").write_text("{}")
            (scrubbed / "review_status.json").write_text(
                '{"status":"pending_review"}'
            )
            db.update_capture("test-id", scrubbed_path=str(scrubbed))
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
        self,
        from_status: ReviewStatus,
        to_status: ReviewStatus,
    ) -> None:
        """Invalid transitions should raise ValueError."""
        with pytest.raises(ValueError):
            transition_status("test-id", from_status, to_status)


class TestCheckEgress:
    """Tests for the egress check function."""

    def test_egress_allowed_reviewed(self, db: IndexDB, tmp_path: Path) -> None:
        """Reviewed captures expose only the distinct sanitized derivative."""
        raw = tmp_path / "cap"
        scrubbed = tmp_path / "cap.scrubbed"
        raw.mkdir()
        scrubbed.mkdir()
        (scrubbed / "scrub_manifest.json").write_text(
            '{"scrub_level":"standard"}'
        )
        from engine.review import derivative_tree_sha256

        (scrubbed / "review_status.json").write_text(
            '{"status":"reviewed","approved_tree_sha256":"'
            + derivative_tree_sha256(scrubbed)
            + '"}'
        )
        db.insert_capture("test-id", str(raw), "2026-03-02T10:00:00Z")
        db.update_capture("test-id", review_status="reviewed")
        db.update_capture("test-id", scrubbed_path=str(scrubbed))
        assert check_egress_allowed("test-id", db) is True
        assert approved_egress_path("test-id", db) == scrubbed.resolve()

    def test_egress_blocked_dismissed(self, db: IndexDB) -> None:
        """A local dismissal never grants raw-data egress."""
        db.insert_capture("test-id", "/tmp/cap", "2026-03-02T10:00:00Z")
        db.update_capture("test-id", review_status="dismissed")
        with pytest.raises(EgressBlockedError):
            check_egress_allowed("test-id", db)

    def test_egress_blocked_when_reviewed_row_has_no_derivative(
        self,
        db: IndexDB,
        tmp_path: Path,
    ) -> None:
        raw = tmp_path / "cap"
        raw.mkdir()
        db.insert_capture("test-id", str(raw), "2026-03-02T10:00:00Z")
        db.update_capture("test-id", review_status="reviewed")
        with pytest.raises(EgressArtifactError, match="no approved sanitized"):
            check_egress_allowed("test-id", db)

    def test_egress_blocked_when_derivative_points_to_raw(
        self,
        db: IndexDB,
        tmp_path: Path,
    ) -> None:
        raw = tmp_path / "cap"
        raw.mkdir()
        db.insert_capture("test-id", str(raw), "2026-03-02T10:00:00Z")
        db.update_capture("test-id", review_status="reviewed", scrubbed_path=str(raw))
        with pytest.raises(EgressArtifactError, match="raw data"):
            check_egress_allowed("test-id", db)

    def test_egress_blocked_when_derivative_contains_symlink(
        self,
        db: IndexDB,
        tmp_path: Path,
    ) -> None:
        raw = tmp_path / "cap"
        scrubbed = tmp_path / "cap.scrubbed"
        raw.mkdir()
        scrubbed.mkdir()
        (scrubbed / "escape").symlink_to(raw, target_is_directory=True)
        db.insert_capture("test-id", str(raw), "2026-03-02T10:00:00Z")
        db.update_capture("test-id", review_status="reviewed", scrubbed_path=str(scrubbed))
        with pytest.raises(EgressArtifactError, match="symlink"):
            check_egress_allowed("test-id", db)

    def test_review_binds_exact_derivative_bytes(
        self,
        db: IndexDB,
        tmp_path: Path,
    ) -> None:
        raw = tmp_path / "cap"
        scrubbed = tmp_path / "cap.scrubbed"
        raw.mkdir()
        scrubbed.mkdir()
        (scrubbed / "scrub_manifest.json").write_text("{}")
        (scrubbed / "review_status.json").write_text(
            '{"status":"pending_review"}'
        )
        artifact = scrubbed / "data.bin"
        artifact.write_bytes(b"reviewed")
        db.insert_capture("test-id", str(raw), "2026-03-02T10:00:00Z")
        db.update_capture(
            "test-id",
            review_status="scrubbed",
            scrubbed_path=str(scrubbed),
        )

        transition_status(
            "test-id",
            ReviewStatus.SCRUBBED,
            ReviewStatus.REVIEWED,
            db=db,
        )
        assert check_egress_allowed("test-id", db) is True

        artifact.write_bytes(b"changed after review")
        with pytest.raises(EgressArtifactError, match="changed after"):
            check_egress_allowed("test-id", db)

    def test_egress_blocks_hard_linked_derivative_file(
        self,
        db: IndexDB,
        tmp_path: Path,
    ) -> None:
        raw = tmp_path / "cap"
        scrubbed = tmp_path / "cap.scrubbed"
        raw.mkdir()
        scrubbed.mkdir()
        source = tmp_path / "outside-secret"
        source.write_bytes(b"secret")
        (scrubbed / "linked").hardlink_to(source)
        (scrubbed / "scrub_manifest.json").write_text("{}")
        (scrubbed / "review_status.json").write_text("{}")
        db.insert_capture("test-id", str(raw), "2026-03-02T10:00:00Z")
        db.update_capture(
            "test-id", review_status="reviewed", scrubbed_path=str(scrubbed)
        )

        with pytest.raises(EgressArtifactError, match="hard-linked"):
            check_egress_allowed("test-id", db)

    def test_basic_scrubbed_screenshot_is_blocked_even_without_raw_screenshot(
        self,
        db: IndexDB,
        tmp_path: Path,
    ) -> None:
        from engine.review import derivative_tree_sha256

        raw = tmp_path / "cap"
        scrubbed = tmp_path / "cap.scrubbed"
        raw.mkdir()
        (scrubbed / "screenshots").mkdir(parents=True)
        (scrubbed / "screenshots" / "frame.png").write_bytes(b"raw screenshot")
        (scrubbed / "scrub_manifest.json").write_text('{"scrub_level":"basic"}')
        (scrubbed / "review_status.json").write_text(
            '{"status":"reviewed","approved_tree_sha256":"'
            + derivative_tree_sha256(scrubbed)
            + '"}'
        )
        db.insert_capture("test-id", str(raw), "2026-03-02T10:00:00Z")
        db.update_capture(
            "test-id", review_status="reviewed", scrubbed_path=str(scrubbed)
        )

        with pytest.raises(EgressArtifactError, match="Image-capable"):
            check_egress_allowed("test-id", db)

    def test_regular_file_cannot_replace_derivative_directory(
        self,
        db: IndexDB,
        tmp_path: Path,
    ) -> None:
        raw = tmp_path / "cap"
        scrubbed = tmp_path / "cap.scrubbed"
        raw.mkdir()
        scrubbed.write_bytes(b"raw replacement")
        db.insert_capture("test-id", str(raw), "2026-03-02T10:00:00Z")
        db.update_capture(
            "test-id", review_status="reviewed", scrubbed_path=str(scrubbed)
        )

        with pytest.raises(EgressArtifactError, match="directory"):
            check_egress_allowed("test-id", db)

    def test_approval_file_rejects_extra_free_text(
        self,
        db: IndexDB,
        tmp_path: Path,
    ) -> None:
        from engine.review import derivative_tree_sha256

        raw = tmp_path / "cap"
        scrubbed = tmp_path / "cap.scrubbed"
        raw.mkdir()
        scrubbed.mkdir()
        (scrubbed / "scrub_manifest.json").write_text(
            '{"scrub_level":"standard"}'
        )
        digest = derivative_tree_sha256(scrubbed)
        (scrubbed / "review_status.json").write_text(
            '{"status":"reviewed","approved_tree_sha256":"'
            + digest
            + '","secret":"Jane Doe record 12345"}'
        )
        db.insert_capture("test-id", str(raw), "2026-03-02T10:00:00Z")
        db.update_capture(
            "test-id", review_status="reviewed", scrubbed_path=str(scrubbed)
        )

        with pytest.raises(EgressArtifactError, match="schema"):
            check_egress_allowed("test-id", db)

    def test_egress_blocked_when_derivative_is_symlink(
        self,
        db: IndexDB,
        tmp_path: Path,
    ) -> None:
        raw = tmp_path / "cap"
        derivative_target = tmp_path / "cap.scrubbed.target"
        derivative_link = tmp_path / "cap.scrubbed"
        raw.mkdir()
        derivative_target.mkdir()
        derivative_link.symlink_to(derivative_target, target_is_directory=True)
        db.insert_capture("test-id", str(raw), "2026-03-02T10:00:00Z")
        db.update_capture(
            "test-id",
            review_status="reviewed",
            scrubbed_path=str(derivative_link),
        )
        with pytest.raises(EgressArtifactError, match="symlink"):
            check_egress_allowed("test-id", db)

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
