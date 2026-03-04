"""Tests for the PII scrubber."""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.scrubber import Scrubber, ScrubLevel


class TestScrubber:
    """Tests for PII scrubbing operations."""

    def test_scrub_text_basic_detects_email(self) -> None:
        """Basic scrubbing should detect and redact email addresses."""
        scrubber = Scrubber(level=ScrubLevel.BASIC)
        text = "Contact me at john.doe@example.com for details"
        scrubbed, redactions = scrubber.scrub_text(text)
        assert "john.doe@example.com" not in scrubbed
        assert "<EMAIL_ADDRESS>" in scrubbed
        assert len(redactions) >= 1
        assert any(r["entity"] == "EMAIL_ADDRESS" for r in redactions)

    def test_scrub_text_basic_detects_credit_card(self) -> None:
        """Basic scrubbing should detect and redact credit card numbers."""
        scrubber = Scrubber(level=ScrubLevel.BASIC)
        text = "Card number: 4111-1111-1111-1111"
        scrubbed, redactions = scrubber.scrub_text(text)
        assert "4111-1111-1111-1111" not in scrubbed
        assert "<CREDIT_CARD>" in scrubbed
        assert any(r["entity"] == "CREDIT_CARD" for r in redactions)

    def test_scrub_text_basic_detects_ssn(self) -> None:
        """Basic scrubbing should detect and redact SSNs."""
        scrubber = Scrubber(level=ScrubLevel.BASIC)
        text = "SSN: 123-45-6789"
        scrubbed, redactions = scrubber.scrub_text(text)
        assert "123-45-6789" not in scrubbed
        assert "<SSN>" in scrubbed

    def test_scrub_text_basic_detects_phone(self) -> None:
        """Basic scrubbing should detect phone numbers."""
        scrubber = Scrubber(level=ScrubLevel.BASIC)
        text = "Call me at (555) 123-4567"
        scrubbed, redactions = scrubber.scrub_text(text)
        assert "123-4567" not in scrubbed

    def test_scrub_text_basic_detects_ip(self) -> None:
        """Basic scrubbing should detect IP addresses."""
        scrubber = Scrubber(level=ScrubLevel.BASIC)
        text = "Server IP: 192.168.1.100"
        scrubbed, redactions = scrubber.scrub_text(text)
        assert "192.168.1.100" not in scrubbed
        assert "<IP_ADDRESS>" in scrubbed

    def test_scrub_text_basic_no_false_positives(self) -> None:
        """Clean text should not be modified."""
        scrubber = Scrubber(level=ScrubLevel.BASIC)
        text = "This is a normal sentence with no PII."
        scrubbed, redactions = scrubber.scrub_text(text)
        assert scrubbed == text
        assert len(redactions) == 0

    def test_scrub_text_returns_redaction_records(self) -> None:
        """Redaction records should have required fields."""
        scrubber = Scrubber(level=ScrubLevel.BASIC)
        text = "Email: test@example.com"
        _, redactions = scrubber.scrub_text(text)
        assert len(redactions) >= 1
        r = redactions[0]
        assert "entity" in r
        assert "start" in r
        assert "end" in r
        assert "text_hash" in r

    def test_scrub_capture_creates_scrubbed_directory(
        self, sample_capture_dir: Path,
    ) -> None:
        """Scrubbing a capture should create a parallel .scrubbed/ directory."""
        scrubber = Scrubber(level=ScrubLevel.BASIC)
        scrubbed_path = scrubber.scrub_capture(sample_capture_dir)
        assert scrubbed_path.exists()
        assert scrubbed_path.name.endswith(".scrubbed")

    def test_scrub_capture_writes_manifest(
        self, sample_capture_dir: Path,
    ) -> None:
        """Scrubbing should create scrub_manifest.json."""
        scrubber = Scrubber(level=ScrubLevel.BASIC)
        scrubbed_path = scrubber.scrub_capture(sample_capture_dir)
        manifest_path = scrubbed_path / "scrub_manifest.json"
        assert manifest_path.exists()
        import json

        manifest = json.loads(manifest_path.read_text())
        assert manifest["scrub_level"] == "basic"
        assert "total_redactions" in manifest

    def test_scrub_capture_writes_review_status(
        self, sample_capture_dir: Path,
    ) -> None:
        """Scrubbing should create review_status.json."""
        scrubber = Scrubber(level=ScrubLevel.BASIC)
        scrubbed_path = scrubber.scrub_capture(sample_capture_dir)
        status_path = scrubbed_path / "review_status.json"
        assert status_path.exists()

    def test_scrub_capture_nonexistent_raises(self) -> None:
        """Scrubbing a nonexistent path should raise FileNotFoundError."""
        scrubber = Scrubber(level=ScrubLevel.BASIC)
        with pytest.raises(FileNotFoundError):
            scrubber.scrub_capture(Path("/nonexistent/path"))
