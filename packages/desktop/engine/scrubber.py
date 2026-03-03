"""PII scrubbing orchestration -- creates scrubbed copies of raw captures.

The recording pipeline saves raw, unscrubbed data to local disk. Scrubbing
is performed on demand (when the user prepares a recording for upload),
creating a parallel scrubbed copy without modifying the original.

Scrubbing levels (from design doc Section 5.2):
    Basic:    Regex only (credit cards, SSNs, emails, phones, IPs). <1ms per text.
    Standard: Presidio + spaCy en_core_web_sm (+ names, locations, dates).
    Enhanced: Presidio + spaCy en_core_web_trf (best NER accuracy).

Scrubbed copy format (Section 5.4):
    captures/<session>.scrubbed/
        events.db              Text events redacted
        screenshots/*.png      PII regions blurred/filled
        scrub_manifest.json    What was scrubbed, where, why
        review_status.json     User approvals/rejections

The scrub_manifest.json enables the review UI to show exactly what changed.
"""

from __future__ import annotations

import enum
from pathlib import Path


class ScrubLevel(enum.Enum):
    """PII scrubbing level."""

    BASIC = "basic"
    STANDARD = "standard"
    ENHANCED = "enhanced"


class Scrubber:
    """Orchestrates PII scrubbing of capture sessions.

    Creates a parallel `.scrubbed/` directory with redacted copies of
    events and screenshots without modifying the original capture.

    Args:
        level: Scrubbing level to apply.
    """

    def __init__(self, level: ScrubLevel = ScrubLevel.BASIC) -> None:
        self.level = level

    def scrub_capture(self, capture_path: Path) -> Path:
        """Run PII scrubbing on a capture session.

        Creates a parallel `.scrubbed/` directory alongside the capture.

        Args:
            capture_path: Path to the raw capture directory.

        Returns:
            Path to the scrubbed copy directory.

        Raises:
            FileNotFoundError: If the capture directory does not exist.
        """
        # TODO: Create <capture>.scrubbed/ directory
        # TODO: Scrub text events from events.db -> scrubbed events.db
        # TODO: Scrub screenshots (OCR + NER + redaction if standard/enhanced)
        # TODO: Generate scrub_manifest.json with redaction details
        # TODO: Initialize review_status.json
        raise NotImplementedError

    def scrub_text(self, text: str) -> tuple[str, list[dict]]:
        """Scrub PII from a text string.

        Args:
            text: Input text potentially containing PII.

        Returns:
            Tuple of (scrubbed text, list of redaction records).
        """
        if self.level == ScrubLevel.BASIC:
            return self._scrub_text_regex(text)
        else:
            return self._scrub_text_presidio(text)

    def scrub_image(self, image_path: Path, output_path: Path) -> list[dict]:
        """Scrub PII from a screenshot image.

        Args:
            image_path: Path to the input image.
            output_path: Path to write the scrubbed image.

        Returns:
            List of redacted region records.
        """
        # TODO: Use openadapt-privacy for image scrubbing
        # TODO: OCR -> detect PII regions -> fill/blur regions
        raise NotImplementedError

    def _scrub_text_regex(self, text: str) -> tuple[str, list[dict]]:
        """Scrub PII using regex patterns only (basic level).

        Detects: credit cards, SSNs, email addresses, phone numbers, IP addresses.

        Args:
            text: Input text.

        Returns:
            Tuple of (scrubbed text, list of redaction records).
        """
        # TODO: Apply regex patterns for each PII type
        # TODO: Replace matches with type-specific placeholders
        # TODO: Return scrubbed text and redaction records
        raise NotImplementedError

    def _scrub_text_presidio(self, text: str) -> tuple[str, list[dict]]:
        """Scrub PII using Presidio NER (standard/enhanced level).

        Requires openadapt-privacy to be installed.

        Args:
            text: Input text.

        Returns:
            Tuple of (scrubbed text, list of redaction records).
        """
        # TODO: Use openadapt-privacy Presidio integration
        # TODO: Configure analyzer based on scrub level (sm vs trf model)
        raise NotImplementedError
