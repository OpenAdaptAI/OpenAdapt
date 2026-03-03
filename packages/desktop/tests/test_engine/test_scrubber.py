"""Tests for the PII scrubber."""

from __future__ import annotations

import pytest

from engine.scrubber import Scrubber, ScrubLevel


class TestScrubber:
    """Tests for PII scrubbing operations."""

    @pytest.mark.skip(reason="Not yet implemented")
    def test_scrub_text_basic_detects_email(self) -> None:
        """Basic scrubbing should detect and redact email addresses."""
        scrubber = Scrubber(level=ScrubLevel.BASIC)
        text = "Contact me at john.doe@example.com for details"
        scrubbed, redactions = scrubber.scrub_text(text)
        assert "john.doe@example.com" not in scrubbed
        assert len(redactions) == 1
        assert redactions[0]["entity"] == "EMAIL_ADDRESS"

    @pytest.mark.skip(reason="Not yet implemented")
    def test_scrub_text_basic_detects_credit_card(self) -> None:
        """Basic scrubbing should detect and redact credit card numbers."""
        scrubber = Scrubber(level=ScrubLevel.BASIC)
        text = "Card number: 4111-1111-1111-1111"
        scrubbed, redactions = scrubber.scrub_text(text)
        assert "4111-1111-1111-1111" not in scrubbed
        assert len(redactions) == 1
        assert redactions[0]["entity"] == "CREDIT_CARD"

    @pytest.mark.skip(reason="Not yet implemented")
    def test_scrub_text_basic_detects_ssn(self) -> None:
        """Basic scrubbing should detect and redact SSNs."""
        scrubber = Scrubber(level=ScrubLevel.BASIC)
        text = "SSN: 123-45-6789"
        scrubbed, redactions = scrubber.scrub_text(text)
        assert "123-45-6789" not in scrubbed

    @pytest.mark.skip(reason="Not yet implemented")
    def test_scrub_capture_creates_scrubbed_directory(
        self, sample_capture_dir,
    ) -> None:
        """Scrubbing a capture should create a parallel .scrubbed/ directory."""
        scrubber = Scrubber(level=ScrubLevel.BASIC)
        scrubbed_path = scrubber.scrub_capture(sample_capture_dir)
        assert scrubbed_path.exists()
        assert scrubbed_path.name.endswith(".scrubbed")
