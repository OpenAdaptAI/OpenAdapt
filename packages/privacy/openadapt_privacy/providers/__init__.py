"""Scrubbing providers package.

This module provides a registry of available scrubbing providers and
a factory method for instantiating them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openadapt_privacy.base import ScrubbingProvider


class ScrubProvider:
    """Registry of available scrubbing providers."""

    PRESIDIO = "PRESIDIO"
    COMPREHEND = "COMPREHEND"
    PRIVATE_AI = "PRIVATE_AI"

    @classmethod
    def as_options(cls) -> dict[str, str]:
        """Return the available options as a dict of provider ID to display name.

        Returns:
            Dictionary mapping provider IDs to human-readable names.
        """
        return {
            cls.PRESIDIO: "Presidio",
            # Comprehend does not support the scrub_image method
            # cls.COMPREHEND: "Comprehend",
            cls.PRIVATE_AI: "Private AI",
        }

    @classmethod
    def get_available_providers(cls) -> list[str]:
        """Return the list of available provider IDs.

        Returns:
            List of provider ID strings.
        """
        return [cls.PRESIDIO, cls.PRIVATE_AI]

    @classmethod
    def get_scrubber(cls, provider: str) -> "ScrubbingProvider":
        """Return a scrubber instance for the given provider.

        Args:
            provider: The provider ID to instantiate.

        Returns:
            An instance of the requested scrubbing provider.

        Raises:
            ValueError: If the provider is not supported.
        """
        if provider not in cls.get_available_providers():
            raise ValueError(f"Provider {provider} is not supported.")

        if provider == cls.PRESIDIO:
            from openadapt_privacy.providers.presidio import PresidioScrubbingProvider

            return PresidioScrubbingProvider()
        elif provider == cls.PRIVATE_AI:
            raise NotImplementedError(
                "Private AI provider is not yet implemented in openadapt-privacy. "
                "Please use PRESIDIO or contribute an implementation."
            )

        raise ValueError(f"Unknown provider: {provider}")
