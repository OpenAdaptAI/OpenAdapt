"""The Base (Parent) Class for Privacy Scrubbing Providers."""

from typing import List

from PIL import Image
from pydantic import BaseModel  # pylint: disable=no-name-in-module

from openadapt.config import config


class Modality:  # pylint: disable=too-few-public-methods
    """A Base Class for Modality Types."""

    TEXT = "TEXT"
    PIL_IMAGE = "PIL_IMAGE"
    PDF = "PDF"
    MP4 = "MP4"


class ScrubbingProvider(BaseModel):
    """A Base Class for Scrubbing Providers."""

    name: str
    capabilities: List[str]

    class Config:  # pylint: disable=too-few-public-methods
        """Pydantic Config Class."""

        arbitrary_types_allowed = True

    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        """Scrub the text of all PII/PHI.

        Args:
            text (str): Text to be scrubbed
            is_separated (bool): Whether the text is separated with special characters

        Returns:
            str: Scrubbed text
        """
        raise NotImplementedError

    def scrub_image(
        self,
        image: Image,
        fill_color: int = config.SCRUB_FILL_COLOR,  # pylint: disable=no-member
    ) -> Image:
        """Scrub the image of all PII/PHI.

        Args:
            image (Image): A PIL.Image object to be scrubbed
            fill_color (int): The color used to fill the redacted regions(BGR).

        Returns:
            Image: The scrubbed image with PII and PHI removed.
        """
        raise NotImplementedError

    def scrub_pdf(self, path_to_pdf: str) -> str:
        """Scrub the PDF of all PII/PHI.

        Args:
            path_to_pdf (str): Path to the PDF to be scrubbed

        Returns:
            str: Path to the scrubbed PDF
        """
        raise NotImplementedError

    def scrub_mp4(  # pylint: disable=too-many-arguments
        self,
        mp4_file: str,
        scrub_all_entities: bool = False,
        playback_speed_multiplier: float = 1.0,
        crop_start_time: int = 0,
        crop_end_time: int | None = None,
    ) -> str:
        """Scrub a mp4 file.

        Args:
            mp4_file_path: Path to the mp4 file.
            scrub_all_entities: True/False. If true, scrubs all entities
            playback_speed_multiplier: Multiplier for playback speed. (float/int)
            crop_start_time: Start Time (in seconds)
            end_start_time: End Time (in seconds)

        Returns:
            Path to the scrubbed (redacted) mp4 file.
        """
        raise NotImplementedError


class TextScrubbingMixin:
    """Mixin class to add scrub_text method."""

    def scrub_text_all(self, text: str) -> str:
        """Scrub the text by replacing all characters with config.SCRUB_CHAR.

        Args:
            text (str): Text to be scrubbed

        Returns:
            str: Scrubbed text
        """
        return config.SCRUB_CHAR * len(text)  # pylint: disable=E1101

    def scrub_dict(
        self,
        input_dict: dict,
        list_keys: list = None,
        scrub_all: bool = False,
        force_scrub_children: bool = False,
    ) -> dict:
        """Scrub the dict of all PII/PHI using Presidio ANALYZER.TRF and Anonymizer.

        Args:
            input_dict (dict): A dict to be scrubbed
            list_keys (list): List of keys to be scrubbed
            scrub_all (bool): Whether to scrub all sub-fields/keys/values
            of that particular key
            force_scrub_children (bool): Whether to force scrub children
            even if key is not present

        Returns:
            dict: The scrubbed dict with PII and PHI removed.
        """
        if list_keys is None:
            list_keys = config.SCRUB_KEYS_HTML  # pylint: disable=E1101

        scrubbed_dict = {}
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
                        self._scrub_list_item(
                            item, key, list_keys, force_scrub_children
                        )
                        if self._should_scrub_list_item(item, key, list_keys)
                        else item
                    )
                    for item in value
                ]
                scrubbed_dict[key] = scrubbed_list
                force_scrub_children = False
            elif isinstance(value, dict):
                if isinstance(key, str) and key == "state":
                    scrubbed_dict[key] = self.scrub_dict(
                        value, list_keys, scrub_all=True
                    )
                else:
                    scrubbed_dict[key] = self.scrub_dict(value, list_keys)
            else:
                scrubbed_dict[key] = value

        return scrubbed_dict

    def scrub_list_dicts(
        self, input_list: list[dict], list_keys: list = None
    ) -> list[dict]:
        """Scrub list of dicts to remove PII/PHI.

        Args:
            input_list (list[dict]): A list of dicts to be scrubbed
            list_keys (list): List of keys to be scrubbed

        Returns:
            list[dict]: The scrubbed list of dicts with PII and PHI removed.
        """
        scrubbed_list_dicts = []
        for input_dict in input_list:
            scrubbed_list_dicts.append(self.scrub_dict(input_dict, list_keys))

        return scrubbed_list_dicts

    def _should_scrub_text(
        self,
        key: str,
        value: str,
        list_keys: list[str],
        scrub_all: bool = False,
    ) -> bool:
        """Check if the key and value should be scrubbed and are of correct instance.

        Args:
            key (str): The key of the item.
            value (str): The value of the item.
            list_keys (list[str]): A list of keys that need to be scrubbed.
            scrub_all (bool): Whether to scrub all sub-fields/keys/values
            of that particular key.

        Returns:
            bool: True if the key and value should be scrubbed, False otherwise.
        """
        return (
            isinstance(value, str)
            and isinstance(key, str)
            and (key in list_keys or scrub_all)
        )

    def _is_scrubbed(self, old_text: str, new_text: str) -> bool:
        """Check if the text has been scrubbed.

        Args:
            old_text (str): The original text
            new_text (str): The scrubbed text

        Returns:
            bool: True if the text has been scrubbed, False otherwise
        """
        return old_text != new_text

    def _scrub_text_item(
        self, value: str, key: str, force_scrub_children: bool = False
    ) -> str:
        """Scrubs the value of a text item.

        Args:
            value (str): The value of the item
            key (str): The key of the item

        Returns:
            str: The scrubbed value
        """
        if key in ("text", "canonical_text"):
            return self.scrub_text(value, is_separated=True)
        if force_scrub_children:
            return self.scrub_text_all(value)
        return self.scrub_text(value)

    def _should_scrub_list_item(
        self, item: str, key: str, list_keys: list[str]
    ) -> bool:
        """Check if the key and item should be scrubbed and are of correct instance.

        Args:
            item (str): The value of the item
            key (str): The key of the item
            list_keys (list): A list of keys that are needed to be scrubbed

        Returns:
            bool: True if the key and value should be scrubbed, False otherwise
        """
        return isinstance(item, (str)) and isinstance(key, str) and key in list_keys

    def _scrub_list_item(
        self,
        item: str | dict,
        key: str,
        list_keys: list[str],
        force_scrub_children: bool = False,
    ) -> str | dict:
        """Scrubs the value of a dict item.

        Args:
            item (str/dict): The value of the dict item
            key (str): The key of the dict item
            list_keys (list): A list of keys that are needed to be scrubbed

        Returns:
            dict/str: The scrubbed dict/value respectively
        """
        if isinstance(item, dict):
            return self.scrub_dict(
                item, list_keys, force_scrub_children=force_scrub_children
            )
        return self._scrub_text_item(item, key)


class ScrubbingProviderFactory:  # pylint: disable=too-few-public-methods
    """A Factory Class for Scrubbing Providers."""

    @staticmethod
    def get_for_modality(
        modality: Modality,
    ) -> List[ScrubbingProvider]:
        """Get Scrubbing Providers for a given Modality.

        Args:
            modality (Modality): Modality Type

        Returns:
            List[ScrubbingProvider]: Scrubbing Providers
        """
        scrubbing_providers = ScrubbingProvider.__subclasses__()

        filtered_providers = [
            provider()
            for provider in scrubbing_providers
            if modality in provider().capabilities
        ]

        return filtered_providers
