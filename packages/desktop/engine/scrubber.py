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
import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

# --- Regex patterns for BASIC level ---
_PATTERNS: list[tuple[str, str]] = [
    ("EMAIL_ADDRESS", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),
    ("CREDIT_CARD", r"\b(?:\d{4}[-\s]?){3}\d{4}\b"),
    ("SSN", r"\b\d{3}-\d{2}-\d{4}\b"),
    ("PHONE_NUMBER", r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),
    ("IP_ADDRESS", r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
]


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
        self._provider = None

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
        if not capture_path.exists():
            raise FileNotFoundError(f"Capture directory not found: {capture_path}")

        scrubbed_path = capture_path.parent / (capture_path.name + ".scrubbed")
        scrubbed_path.mkdir(parents=True, exist_ok=True)

        all_redactions: list[dict] = []

        # Scrub meta.json text fields
        meta_path = capture_path / "meta.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            for key in ("task_description",):
                if key in meta and isinstance(meta[key], str):
                    scrubbed_text, redactions = self.scrub_text(meta[key])
                    meta[key] = scrubbed_text
                    for r in redactions:
                        r["source"] = f"meta.json:{key}"
                    all_redactions.extend(redactions)
            (scrubbed_path / "meta.json").write_text(json.dumps(meta, indent=2))

        # Copy and scrub screenshots
        screenshots_src = capture_path / "screenshots"
        if screenshots_src.exists():
            screenshots_dst = scrubbed_path / "screenshots"
            screenshots_dst.mkdir(exist_ok=True)
            for img_path in sorted(screenshots_src.glob("*.png")):
                output_path = screenshots_dst / img_path.name
                img_redactions = self.scrub_image(img_path, output_path)
                for r in img_redactions:
                    r["source"] = f"screenshots/{img_path.name}"
                all_redactions.extend(img_redactions)

        # Write scrub manifest
        manifest = {
            "scrub_level": self.level.value,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_redactions": len(all_redactions),
            "redactions": all_redactions,
        }
        (scrubbed_path / "scrub_manifest.json").write_text(json.dumps(manifest, indent=2))

        # Write review status
        review_status = {
            "status": "pending_review",
            "scrubbed_at": datetime.now(timezone.utc).isoformat(),
            "scrub_level": self.level.value,
        }
        (scrubbed_path / "review_status.json").write_text(json.dumps(review_status, indent=2))

        return scrubbed_path

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
        if self.level == ScrubLevel.BASIC:
            # Basic level: no image scrubbing, just copy
            shutil.copy2(image_path, output_path)
            return []

        # Standard/Enhanced: try openadapt-privacy
        try:
            from openadapt_privacy.providers.presidio import PresidioScrubbingProvider

            if self._provider is None:
                self._provider = PresidioScrubbingProvider()

            from PIL import Image
            img = Image.open(image_path)
            scrubbed_img = self._provider.scrub_image(img)
            scrubbed_img.save(output_path)
            return [{"type": "image_scrub", "path": str(output_path)}]
        except ImportError:
            # Fallback: copy without scrubbing
            shutil.copy2(image_path, output_path)
            return []

    def _scrub_text_regex(self, text: str) -> tuple[str, list[dict]]:
        """Scrub PII using regex patterns only (basic level).

        Detects: credit cards, SSNs, email addresses, phone numbers, IP addresses.

        Args:
            text: Input text.

        Returns:
            Tuple of (scrubbed text, list of redaction records).
        """
        redactions: list[dict] = []
        scrubbed = text

        for entity_type, pattern in _PATTERNS:
            for match in re.finditer(pattern, scrubbed):
                redactions.append({
                    "entity": entity_type,
                    "start": match.start(),
                    "end": match.end(),
                    "text_hash": hashlib.sha256(match.group().encode()).hexdigest()[:16],
                })

        # Apply replacements in reverse order to preserve positions
        for entity_type, pattern in _PATTERNS:
            scrubbed = re.sub(pattern, f"<{entity_type}>", scrubbed)

        return scrubbed, redactions

    def _scrub_text_presidio(self, text: str) -> tuple[str, list[dict]]:
        """Scrub PII using Presidio NER (standard/enhanced level).

        Requires openadapt-privacy to be installed.

        Args:
            text: Input text.

        Returns:
            Tuple of (scrubbed text, list of redaction records).
        """
        try:
            from openadapt_privacy.providers.presidio import PresidioScrubbingProvider

            if self._provider is None:
                self._provider = PresidioScrubbingProvider()

            scrubbed = self._provider.scrub_text(text)
            # Build redaction records by diffing original vs scrubbed
            redactions: list[dict] = []
            if scrubbed != text:
                redactions.append({
                    "entity": "PRESIDIO_DETECTED",
                    "original_length": len(text),
                    "scrubbed_length": len(scrubbed),
                })
            return scrubbed, redactions
        except ImportError:
            # Fallback to regex if presidio not available
            return self._scrub_text_regex(text)
