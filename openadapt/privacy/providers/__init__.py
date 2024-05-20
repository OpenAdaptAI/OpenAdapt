"""Package for the Providers.

Module: __init__.py
"""


class ScrubProvider:
    """A Class for Scrubbing Provider."""

    PRESIDIO = "PRESIDIO"
    COMPREHEND = "COMPREHEND"
    PRIVATE_AI = "PRIVATE_AI"

    @classmethod
    def as_options(cls: "ScrubProvider") -> dict:
        """Return the available options."""
        return {
            cls.PRESIDIO: "Presidio",
            # cls.COMPREHEND: "Comprehend", this doesn't have the scrub_image method yet
            cls.PRIVATE_AI: "Private AI",
        }

    @classmethod
    def get_available_providers(cls: "ScrubProvider") -> list:
        """Return the available providers."""
        return [cls.PRESIDIO, cls.PRIVATE_AI]

    @classmethod
    def get_scrubber(cls: "ScrubProvider", provider: str) -> "ScrubProvider":
        """Return the scrubber for the provider.

        Args:
            provider: The provider to get the scrubber for.

        Returns:
            The scrubber for the provider.
        """
        if provider not in cls.get_available_providers():
            raise ValueError(f"Provider {provider} is not supported.")
        if provider == cls.PRESIDIO:
            from openadapt.privacy.providers.presidio import PresidioScrubbingProvider

            return PresidioScrubbingProvider()
        elif provider == cls.PRIVATE_AI:
            from openadapt.privacy.providers.private_ai import (
                PrivateAIScrubbingProvider,
            )

            return PrivateAIScrubbingProvider()
