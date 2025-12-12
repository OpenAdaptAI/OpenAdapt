"""Base classes for privacy scrubbing providers.

This module defines the abstract base classes and mixins for implementing
PII/PHI scrubbing providers.
"""

from __future__ import annotations

from typing import Any, List

from PIL import Image
from pydantic import BaseModel

from openadapt_privacy.config import config


class Modality:
    """Supported modality types for scrubbing."""

    TEXT = "TEXT"
    PIL_IMAGE = "PIL_IMAGE"
    PDF = "PDF"
    MP4 = "MP4"


class ScrubbingProvider(BaseModel):
    """Abstract base class for scrubbing providers.

    Subclasses must implement the scrub methods for their supported modalities.
    """

    name: str
    capabilities: List[str]

    model_config = {"arbitrary_types_allowed": True}

    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        """Scrub PII/PHI from text.

        Args:
            text: Text to be scrubbed.
            is_separated: Whether the text contains separated characters
                (e.g., key sequences like "a-b-c").

        Returns:
            Scrubbed text with PII/PHI replaced.

        Raises:
            NotImplementedError: If the provider doesn't support text scrubbing.
        """
        raise NotImplementedError

    def scrub_image(
        self,
        image: Image.Image,
        fill_color: int | None = None,
    ) -> Image.Image:
        """Scrub PII/PHI from an image.

        Args:
            image: PIL Image object to be scrubbed.
            fill_color: BGR color value for redacted regions.
                Defaults to config.SCRUB_FILL_COLOR.

        Returns:
            Scrubbed image with PII/PHI redacted.

        Raises:
            NotImplementedError: If the provider doesn't support image scrubbing.
        """
        raise NotImplementedError

    def scrub_pdf(self, path_to_pdf: str) -> str:
        """Scrub PII/PHI from a PDF file.

        Args:
            path_to_pdf: Path to the PDF file to be scrubbed.

        Returns:
            Path to the scrubbed PDF file.

        Raises:
            NotImplementedError: If the provider doesn't support PDF scrubbing.
        """
        raise NotImplementedError

    def scrub_mp4(
        self,
        mp4_file: str,
        scrub_all_entities: bool = False,
        playback_speed_multiplier: float = 1.0,
        crop_start_time: int = 0,
        crop_end_time: int | None = None,
    ) -> str:
        """Scrub PII/PHI from an MP4 video file.

        Args:
            mp4_file: Path to the MP4 file to be scrubbed.
            scrub_all_entities: If True, scrub all detected entities.
            playback_speed_multiplier: Multiplier for playback speed.
            crop_start_time: Start time in seconds for cropping.
            crop_end_time: End time in seconds for cropping.

        Returns:
            Path to the scrubbed MP4 file.

        Raises:
            NotImplementedError: If the provider doesn't support MP4 scrubbing.
        """
        raise NotImplementedError


class TextScrubbingMixin:
    """Mixin class providing text and dict scrubbing utilities.

    This mixin requires the implementing class to have a `scrub_text` method.
    It provides additional utilities for scrubbing nested dictionaries and lists.
    """

    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        """Scrub PII/PHI from text. Must be implemented by subclass."""
        raise NotImplementedError

    def scrub_text_all(self, text: str) -> str:
        """Replace all characters in text with the scrub character.

        This is a more aggressive scrubbing that replaces the entire text
        rather than just detected PII/PHI entities.

        Args:
            text: Text to be scrubbed.

        Returns:
            Text with all characters replaced by SCRUB_CHAR.
        """
        if text is None:
            return None
        return config.SCRUB_CHAR * len(text)

    def scrub_dict(
        self,
        input_dict: dict[str, Any],
        list_keys: list[str] | None = None,
        scrub_all: bool = False,
        force_scrub_children: bool = False,
    ) -> dict[str, Any]:
        """Scrub PII/PHI from a nested dictionary.

        Recursively processes the dictionary, scrubbing text values for
        specified keys and handling nested structures.

        Args:
            input_dict: Dictionary to be scrubbed.
            list_keys: List of keys whose values should be scrubbed.
                Defaults to config.SCRUB_KEYS_HTML.
            scrub_all: If True, scrub all string values regardless of key.
            force_scrub_children: If True, use aggressive scrubbing for
                child values after PII is detected in parent.

        Returns:
            Scrubbed dictionary with PII/PHI removed.
        """
        if list_keys is None:
            list_keys = config.SCRUB_KEYS_HTML

        scrubbed_dict: dict[str, Any] = {}
        for key, value in input_dict.items():
            if self._should_scrub_text(key, value, list_keys, scrub_all):
                scrubbed_text = self._scrub_text_item(value, key, force_scrub_children)
                if key in ("text", "canonical_text") and self._is_scrubbed(
                    value, scrubbed_text
                ):
                    force_scrub_children = True
                scrubbed_dict[key] = scrubbed_text
            elif isinstance(value, list):
                scrubbed_list = [
                    (
                        self._scrub_list_item(item, key, list_keys, force_scrub_children)
                        if self._should_scrub_list_item(item, key, list_keys)
                        else item
                    )
                    for item in value
                ]
                scrubbed_dict[key] = scrubbed_list
                force_scrub_children = False
            elif isinstance(value, dict):
                if isinstance(key, str) and key == "state":
                    scrubbed_dict[key] = self.scrub_dict(value, list_keys, scrub_all=True)
                else:
                    scrubbed_dict[key] = self.scrub_dict(value, list_keys)
            else:
                scrubbed_dict[key] = value

        return scrubbed_dict

    def scrub_list_dicts(
        self,
        input_list: list[dict[str, Any]],
        list_keys: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Scrub PII/PHI from a list of dictionaries.

        Args:
            input_list: List of dictionaries to be scrubbed.
            list_keys: List of keys whose values should be scrubbed.

        Returns:
            List of scrubbed dictionaries.
        """
        return [self.scrub_dict(input_dict, list_keys) for input_dict in input_list]

    def _should_scrub_text(
        self,
        key: Any,
        value: Any,
        list_keys: list[str],
        scrub_all: bool = False,
    ) -> bool:
        """Check if a key-value pair should be scrubbed.

        Args:
            key: Dictionary key.
            value: Dictionary value.
            list_keys: List of keys that should be scrubbed.
            scrub_all: If True, scrub all string values.

        Returns:
            True if the value should be scrubbed, False otherwise.
        """
        return (
            isinstance(value, str) and isinstance(key, str) and (key in list_keys or scrub_all)
        )

    def _is_scrubbed(self, old_text: str, new_text: str) -> bool:
        """Check if text was modified by scrubbing.

        Args:
            old_text: Original text.
            new_text: Text after scrubbing.

        Returns:
            True if the text was modified, False otherwise.
        """
        return old_text != new_text

    def _scrub_text_item(
        self,
        value: str,
        key: str,
        force_scrub_children: bool = False,
    ) -> str:
        """Scrub a single text value.

        Args:
            value: Text value to scrub.
            key: Dictionary key associated with the value.
            force_scrub_children: If True, use aggressive scrubbing.

        Returns:
            Scrubbed text.
        """
        if key in ("text", "canonical_text"):
            return self.scrub_text(value, is_separated=True)
        if force_scrub_children:
            return self.scrub_text_all(value)
        return self.scrub_text(value)

    def _should_scrub_list_item(
        self,
        item: Any,
        key: str,
        list_keys: list[str],
    ) -> bool:
        """Check if a list item should be scrubbed.

        Args:
            item: List item.
            key: Dictionary key containing the list.
            list_keys: List of keys that should be scrubbed.

        Returns:
            True if the item should be scrubbed, False otherwise.
        """
        return isinstance(item, str) and isinstance(key, str) and key in list_keys

    def _scrub_list_item(
        self,
        item: str | dict[str, Any],
        key: str,
        list_keys: list[str],
        force_scrub_children: bool = False,
    ) -> str | dict[str, Any]:
        """Scrub a single list item.

        Args:
            item: List item (string or dict).
            key: Dictionary key containing the list.
            list_keys: List of keys that should be scrubbed.
            force_scrub_children: If True, use aggressive scrubbing.

        Returns:
            Scrubbed item.
        """
        if isinstance(item, dict):
            return self.scrub_dict(item, list_keys, force_scrub_children=force_scrub_children)
        return self._scrub_text_item(item, key)


class ScrubbingProviderFactory:
    """Factory for creating scrubbing providers based on modality."""

    @staticmethod
    def get_for_modality(modality: str) -> List[ScrubbingProvider]:
        """Get all scrubbing providers that support a given modality.

        Args:
            modality: The modality type (e.g., Modality.TEXT).

        Returns:
            List of provider instances that support the modality.
        """
        scrubbing_providers = ScrubbingProvider.__subclasses__()

        filtered_providers = [
            provider()
            for provider in scrubbing_providers
            if modality in provider().capabilities
        ]

        return filtered_providers
