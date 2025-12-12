"""Tests for Presidio scrubbing provider.

These tests require the presidio optional dependencies to be installed:
    pip install openadapt-privacy[presidio]

And the spaCy model to be downloaded:
    python -m spacy download en_core_web_trf
"""

import pytest

try:
    import spacy

    from openadapt_privacy.config import config

    if not spacy.util.is_package(config.SPACY_MODEL_NAME):
        pytest.skip(
            f"SpaCy model {config.SPACY_MODEL_NAME} not installed",
            allow_module_level=True,
        )

    from openadapt_privacy.providers.presidio import PresidioScrubbingProvider
    from openadapt_privacy.providers import ScrubProvider

    PRESIDIO_AVAILABLE = True
except ImportError:
    PRESIDIO_AVAILABLE = False
    pytest.skip("Presidio dependencies not installed", allow_module_level=True)


@pytest.fixture
def scrubber() -> PresidioScrubbingProvider:
    """Create a PresidioScrubbingProvider instance."""
    return PresidioScrubbingProvider()


class TestPresidioTextScrubbing:
    """Tests for Presidio text scrubbing."""

    def test_scrub_phone_number(self, scrubber: PresidioScrubbingProvider) -> None:
        """Test that phone numbers are scrubbed."""
        text = "My phone number is 123-456-7890."
        result = scrubber.scrub_text(text)
        assert result == "My phone number is <PHONE_NUMBER>."

    def test_scrub_email(self, scrubber: PresidioScrubbingProvider) -> None:
        """Test that email addresses are scrubbed."""
        result = scrubber.scrub_text("My email is john.doe@example.com.")
        assert result == "My email is <EMAIL_ADDRESS>."

    def test_scrub_credit_card(self, scrubber: PresidioScrubbingProvider) -> None:
        """Test that credit card numbers are scrubbed."""
        result = scrubber.scrub_text("My credit card number is 4234-5678-9012-3456 and ")
        assert result == "My credit card number is <CREDIT_CARD> and "

    def test_scrub_ssn(self, scrubber: PresidioScrubbingProvider) -> None:
        """Test that SSNs are scrubbed."""
        result = scrubber.scrub_text("My social security number is 923-45-6789")
        assert result == "My social security number is <US_SSN>"

    def test_scrub_date(self, scrubber: PresidioScrubbingProvider) -> None:
        """Test that dates are scrubbed."""
        result = scrubber.scrub_text("My date of birth is 01/01/2000.")
        assert result == "My date of birth is <DATE_TIME>."

    def test_scrub_person_name(self, scrubber: PresidioScrubbingProvider) -> None:
        """Test that person names are scrubbed."""
        result = scrubber.scrub_text("Contact John Smith for details.")
        assert "<PERSON>" in result

    def test_empty_string(self, scrubber: PresidioScrubbingProvider) -> None:
        """Test empty string returns empty string."""
        assert scrubber.scrub_text("") == ""

    def test_none_input(self, scrubber: PresidioScrubbingProvider) -> None:
        """Test None input returns None."""
        assert scrubber.scrub_text(None) is None

    def test_no_pii(self, scrubber: PresidioScrubbingProvider) -> None:
        """Test text without PII is unchanged."""
        text = "This string doesn't have anything to scrub."
        assert scrubber.scrub_text(text) == text

    def test_multiple_pii_types(self, scrubber: PresidioScrubbingProvider) -> None:
        """Test scrubbing multiple PII types in one text."""
        text = (
            "John Smith's email is johnsmith@example.com and"
            " his phone number is 555-123-4567."
            "His credit card number is 4831-5538-2996-5651 and"
            " his social security number is 923-45-6789."
            " He was born on 01/01/1980."
        )
        result = scrubber.scrub_text(text)

        assert "<PERSON>" in result or "John Smith" not in result
        assert "<EMAIL_ADDRESS>" in result
        assert "<PHONE_NUMBER>" in result
        assert "<CREDIT_CARD>" in result
        assert "<US_SSN>" in result
        assert "<DATE_TIME>" in result


class TestPresidioDictScrubbing:
    """Tests for Presidio dict scrubbing."""

    def test_scrub_dict(self, scrubber: PresidioScrubbingProvider) -> None:
        """Test that scrub_dict works with Presidio provider."""
        input_dict = {"title": "hi my name is Bob Smith."}
        result = scrubber.scrub_dict(input_dict)

        assert "Bob Smith" not in result["title"]
        assert "<PERSON>" in result["title"]


class TestPresidioProviderRegistry:
    """Tests for Presidio provider via ScrubProvider registry."""

    def test_get_presidio_scrubber(self) -> None:
        """Test getting Presidio scrubber from registry."""
        scrubber = ScrubProvider.get_scrubber(ScrubProvider.PRESIDIO)

        assert isinstance(scrubber, PresidioScrubbingProvider)
        assert scrubber.name == "PRESIDIO"

    def test_presidio_capabilities(self) -> None:
        """Test Presidio provider capabilities."""
        scrubber = PresidioScrubbingProvider()

        from openadapt_privacy.base import Modality

        assert Modality.TEXT in scrubber.capabilities
        assert Modality.PIL_IMAGE in scrubber.capabilities
