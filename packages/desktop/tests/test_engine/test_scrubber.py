"""Tests for the PII scrubber."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from engine.scrubber import Scrubber, ScrubbingUnavailableError, ScrubLevel

_PRESIDIO_MODULE = "openadapt_privacy.providers.presidio"


def _png(path: Path) -> Path:
    """Write the smallest valid PNG PIL will open."""
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), "white").save(path)
    return path


def _capture_with_screenshot(root: Path) -> Path:
    """A capture directory holding one screenshot."""
    capture = root / "2026-07-27_09-00-00_pii"
    (capture / "screenshots").mkdir(parents=True)
    _png(capture / "screenshots" / "0001.png")
    (capture / "meta.json").write_text(json.dumps({"task_description": "a@b.com"}))
    return capture


def _break_presidio(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the Presidio provider unimportable, as on a stock install.

    ``openadapt-privacy`` is a hard dependency but Presidio and spaCy are not,
    so this is the shipped default rather than an exotic environment.
    """
    monkeypatch.setitem(sys.modules, _PRESIDIO_MODULE, None)


def _presidio_that_cannot_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provider that imports and constructs but fails when asked to work.

    This is the real observed shape: the module imports without spaCy and only
    raises ``ModuleNotFoundError`` (an ``ImportError``) from ``scrub_*``.
    """

    class _Provider:
        def scrub_text(self, text: str) -> str:
            raise ModuleNotFoundError("No module named 'spacy'")

        def scrub_image(self, image: object) -> object:
            raise ModuleNotFoundError("No module named 'spacy'")

    module = types.ModuleType(_PRESIDIO_MODULE)
    module.PresidioScrubbingProvider = _Provider  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, _PRESIDIO_MODULE, module)


class TestScrubUnavailableIsNotCleanResult:
    """"Could not scrub" must never look like "scrubbed and found nothing"."""

    @pytest.mark.parametrize("level", [ScrubLevel.STANDARD, ScrubLevel.ENHANCED])
    def test_scrub_image_refuses_and_writes_no_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, level: ScrubLevel,
    ) -> None:
        """No provider -> refuse, and leave no unredacted file behind."""
        _break_presidio(monkeypatch)
        source = _png(tmp_path / "in" / "shot.png")
        output = tmp_path / "out" / "shot.png"
        output.parent.mkdir()

        with pytest.raises(ScrubbingUnavailableError):
            Scrubber(level=level).scrub_image(source, output)

        assert not output.exists(), (
            "an unredacted screenshot was copied into the scrubbed output"
        )

    def test_scrub_image_refuses_when_provider_cannot_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Importable-but-broken Presidio refuses too, not just a missing one."""
        _presidio_that_cannot_run(monkeypatch)
        source = _png(tmp_path / "in" / "shot.png")
        output = tmp_path / "out" / "shot.png"
        output.parent.mkdir()

        with pytest.raises(ScrubbingUnavailableError):
            Scrubber(level=ScrubLevel.ENHANCED).scrub_image(source, output)

        assert not output.exists()

    def test_scrub_capture_refuses_before_writing_anything(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A refused scrub leaves no directory that reads as a completed one."""
        _break_presidio(monkeypatch)
        capture = _capture_with_screenshot(tmp_path)

        with pytest.raises(ScrubbingUnavailableError):
            Scrubber(level=ScrubLevel.ENHANCED).scrub_capture(capture)

        scrubbed = capture.parent / (capture.name + ".scrubbed")
        assert not (scrubbed / "scrub_manifest.json").exists(), (
            "a manifest reporting zero redactions was written for a scrub "
            "that never ran"
        )
        assert not (scrubbed / "review_status.json").exists()
        assert not (scrubbed / "screenshots" / "0001.png").exists()

    def test_scrub_text_does_not_silently_downgrade_to_regex(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Enhanced text scrubbing refuses rather than returning regex output.

        A regex result labelled ``enhanced`` in the manifest overstates what
        was checked: it never looked for names, locations, or dates.
        """
        _break_presidio(monkeypatch)

        with pytest.raises(ScrubbingUnavailableError):
            Scrubber(level=ScrubLevel.ENHANCED).scrub_text("Jane Doe, a@b.com")


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

    def test_rescrub_atomically_replaces_stale_derivative_files(
        self, sample_capture_dir: Path,
    ) -> None:
        scrubber = Scrubber(level=ScrubLevel.BASIC)
        scrubbed_path = scrubber.scrub_capture(sample_capture_dir)
        stale = scrubbed_path / "stale-secret.txt"
        stale.write_text("Jane Doe account 12345")

        replacement = scrubber.scrub_capture(sample_capture_dir)

        assert replacement == scrubbed_path
        assert not stale.exists()
        assert (replacement / "scrub_manifest.json").exists()

    def test_manifest_never_contains_identity_bearing_local_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        capture = _capture_with_screenshot(tmp_path)
        identity_name = "Jane-Doe-record-12345"
        named_capture = capture.with_name(identity_name)
        capture.replace(named_capture)
        scrubber = Scrubber(level=ScrubLevel.STANDARD)
        monkeypatch.setattr(scrubber, "_require_provider", lambda: object())
        monkeypatch.setattr(
            scrubber,
            "scrub_text",
            lambda text: ("redacted", []),
        )

        def scrub_image(_source: Path, output: Path) -> list[dict]:
            output.write_bytes(b"redacted")
            return [{"type": "image_scrub"}]

        monkeypatch.setattr(
            scrubber,
            "scrub_image",
            scrub_image,
        )

        derivative = scrubber.scrub_capture(named_capture)

        assert identity_name not in (derivative / "scrub_manifest.json").read_text()

    def test_scrub_capture_nonexistent_raises(self) -> None:
        """Scrubbing a nonexistent path should raise FileNotFoundError."""
        scrubber = Scrubber(level=ScrubLevel.BASIC)
        with pytest.raises(FileNotFoundError):
            scrubber.scrub_capture(Path("/nonexistent/path"))
