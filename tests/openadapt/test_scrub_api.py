import pytest

from openadapt.privacy.base import Modality
from openadapt.privacy.providers import (
    PresidioScrubbingProvider,
    ScrubbingProviderFactory,
)


def test_presidio_scrub_text():
    """Test that PresidioScrubbingProvider can scrub text."""

    data = {
        "name": "Presidio",
        "capabilities": [Modality.TEXT],
    }

    provider = PresidioScrubbingProvider(**data)

    text = "My phone number is 123-456-7890."
    expected_result = "My phone number is <PHONE_NUMBER>."

    scrubbed_text = provider.scrub_text(text)

    assert scrubbed_text == expected_result


# def test_scrubbing_provider_factory():
#     """Test the ScrubbingProviderFactory for Modality.TEXT."""

#     providers = ScrubbingProviderFactory.get_for_modality(Modality.TEXT)

#     # Ensure that we get at least one provider
#     assert providers

#     for provider in providers:
#         # Ensure the provider is an instance of PresidioScrubbingProvider
#         assert isinstance(provider, PresidioScrubbingProvider)

#         # Ensure that the provider supports Modality.TEXT
#         assert Modality.TEXT in provider.capabilities
