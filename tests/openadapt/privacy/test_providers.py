"""A test module for the providers module."""

import pytest
import spacy

from openadapt import config

if not spacy.util.is_package(config.SPACY_MODEL_NAME):  # pylint: disable=no-member
    pytestmark = pytest.mark.skip(reason="SpaCy model not installed!")
else:
    from openadapt.privacy import providers
    from openadapt.privacy.base import Modality, ScrubbingProviderFactory
    from openadapt.privacy.providers.aws_comprehend import ComprehendScrubbingProvider
    from openadapt.privacy.providers.presidio import PresidioScrubbingProvider


def test_scrubbing_provider_factory() -> None:
    """Test the ScrubbingProviderFactory for Modality.TEXT."""
    providers_list = ScrubbingProviderFactory.get_for_modality(Modality.TEXT)

    # Ensure that we get at least one provider
    assert providers_list

    for provider in providers_list:
        # Ensure the provider is an instance of PresidioScrubbingProvider
        assert isinstance(
            provider, providers.presidio.PresidioScrubbingProvider
        ) or isinstance(provider, providers.aws_comprehend.ComprehendScrubbingProvider)

        # Ensure that the provider supports Modality.TEXT
        assert Modality.TEXT in provider.capabilities
