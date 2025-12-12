"""Dict and list scrubbing pipelines.

This module provides convenience functions for scrubbing PII/PHI from
nested dictionaries and lists of dictionaries.
"""

from __future__ import annotations

from typing import Any

from openadapt_privacy.base import ScrubbingProvider, TextScrubbingMixin


class DictScrubber(TextScrubbingMixin):
    """Wrapper class for scrubbing nested dicts using a ScrubbingProvider.

    This class implements the TextScrubbingMixin interface by delegating
    to an underlying ScrubbingProvider instance.

    Example:
        >>> from openadapt_privacy.providers.presidio import PresidioScrubbingProvider
        >>> scrubber = DictScrubber(PresidioScrubbingProvider())
        >>> event = {"text": "Contact john@example.com", "value": "SSN: 123-45-6789"}
        >>> scrubbed = scrubber.scrub_dict(event)
    """

    def __init__(self, scrubber: ScrubbingProvider) -> None:
        """Initialize with a scrubbing provider.

        Args:
            scrubber: The ScrubbingProvider to use for text scrubbing.
        """
        self._scrubber = scrubber

    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        """Scrub PII/PHI from text by delegating to the underlying provider.

        Args:
            text: Text to be scrubbed.
            is_separated: Whether the text contains separated characters.

        Returns:
            Scrubbed text.
        """
        return self._scrubber.scrub_text(text, is_separated=is_separated)


def scrub_dict(
    input_dict: dict[str, Any],
    scrubber: ScrubbingProvider,
    list_keys: list[str] | None = None,
    scrub_all: bool = False,
) -> dict[str, Any]:
    """Scrub PII/PHI from a nested dictionary.

    Convenience function that wraps the TextScrubbingMixin.scrub_dict method.

    Args:
        input_dict: Dictionary to be scrubbed.
        scrubber: The ScrubbingProvider to use for text scrubbing.
        list_keys: List of keys whose values should be scrubbed.
            Defaults to config.SCRUB_KEYS_HTML.
        scrub_all: If True, scrub all string values regardless of key.

    Returns:
        Scrubbed dictionary with PII/PHI removed.

    Example:
        >>> from openadapt_privacy.providers.presidio import PresidioScrubbingProvider
        >>> scrubber = PresidioScrubbingProvider()
        >>> event = {
        ...     "text": "Contact john@example.com",
        ...     "metadata": {"value": "SSN: 123-45-6789"}
        ... }
        >>> scrubbed = scrub_dict(event, scrubber)
    """
    helper = DictScrubber(scrubber)
    return helper.scrub_dict(input_dict, list_keys=list_keys, scrub_all=scrub_all)


def scrub_list_dicts(
    input_list: list[dict[str, Any]],
    scrubber: ScrubbingProvider,
    list_keys: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Scrub PII/PHI from a list of dictionaries.

    Convenience function that wraps the TextScrubbingMixin.scrub_list_dicts method.

    Args:
        input_list: List of dictionaries to be scrubbed.
        scrubber: The ScrubbingProvider to use for text scrubbing.
        list_keys: List of keys whose values should be scrubbed.

    Returns:
        List of scrubbed dictionaries.

    Example:
        >>> from openadapt_privacy.providers.presidio import PresidioScrubbingProvider
        >>> scrubber = PresidioScrubbingProvider()
        >>> events = [
        ...     {"text": "Email: john@example.com"},
        ...     {"text": "Phone: 555-123-4567"},
        ... ]
        >>> scrubbed = scrub_list_dicts(events, scrubber)
    """
    helper = DictScrubber(scrubber)
    return helper.scrub_list_dicts(input_list, list_keys=list_keys)
