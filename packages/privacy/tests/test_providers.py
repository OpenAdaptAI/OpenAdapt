"""Tests for scrubbing providers."""

import pytest

from openadapt_privacy.providers import ScrubProvider


class TestScrubProvider:
    """Tests for ScrubProvider registry."""

    def test_provider_constants(self) -> None:
        """Test that provider constants are defined."""
        assert ScrubProvider.PRESIDIO == "PRESIDIO"
        assert ScrubProvider.COMPREHEND == "COMPREHEND"
        assert ScrubProvider.PRIVATE_AI == "PRIVATE_AI"

    def test_as_options(self) -> None:
        """Test as_options returns correct mapping."""
        options = ScrubProvider.as_options()

        assert isinstance(options, dict)
        assert ScrubProvider.PRESIDIO in options
        assert options[ScrubProvider.PRESIDIO] == "Presidio"
        assert ScrubProvider.PRIVATE_AI in options
        assert options[ScrubProvider.PRIVATE_AI] == "Private AI"

    def test_get_available_providers(self) -> None:
        """Test get_available_providers returns list."""
        providers = ScrubProvider.get_available_providers()

        assert isinstance(providers, list)
        assert ScrubProvider.PRESIDIO in providers
        assert ScrubProvider.PRIVATE_AI in providers

    def test_get_scrubber_invalid_provider(self) -> None:
        """Test get_scrubber raises error for invalid provider."""
        with pytest.raises(ValueError, match="not supported"):
            ScrubProvider.get_scrubber("INVALID_PROVIDER")

    def test_get_scrubber_private_ai_not_implemented(self) -> None:
        """Test get_scrubber raises NotImplementedError for Private AI."""
        with pytest.raises(NotImplementedError, match="Private AI provider"):
            ScrubProvider.get_scrubber(ScrubProvider.PRIVATE_AI)
