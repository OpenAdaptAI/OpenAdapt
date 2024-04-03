"""A test module for the providers module."""

import pytest
import spacy

from openadapt.config import config

if not spacy.util.is_package(config.SPACY_MODEL_NAME):  # pylint: disable=no-member
    pytestmark = pytest.mark.skip(reason="SpaCy model not installed!")
else:
    from openadapt.privacy.base import Modality, ScrubbingProviderFactory
    from openadapt.privacy.providers.aws_comprehend import (  # noqa: F401
        ComprehendScrubbingProvider,
    )
    from openadapt.privacy.providers.presidio import (  # noqa: F401
        PresidioScrubbingProvider,
    )
    from openadapt.privacy.providers.private_ai import (  # noqa: F401
        PrivateAIScrubbingProvider,
    )


def test_scrubbing_provider_factory() -> None:
    """Test the ScrubbingProviderFactory for Modality.TEXT."""
    providers_list = ScrubbingProviderFactory.get_for_modality(Modality.TEXT)

    # Ensure that we get at least one provider
    assert providers_list

    for provider in providers_list:
        # Ensure that the provider supports Modality.TEXT
        assert Modality.TEXT in provider.capabilities
