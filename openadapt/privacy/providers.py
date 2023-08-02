""" A Module for Scrubbing Provider Factory Class and
Other Scrubbing Providers Classes """

from typing import List

from loguru import logger
from PIL import Image
from presidio_analyzer import AnalyzerEngine
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_anonymizer import AnonymizerEngine
from presidio_image_redactor import ImageAnalyzerEngine, ImageRedactorEngine

from openadapt import config
from openadapt.privacy.base import Modality, ScrubbingProvider


class ScrubbingProviderFactory:
    """A Factory Class for Scrubbing Providers"""

    @staticmethod
    def get_for_modality(modality: Modality) -> List[ScrubbingProvider]:
        """Get Scrubbing Providers for a given Modality

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


class PresidioScrubbingProvider(ScrubbingProvider):
    """A Class for Presidio Scrubbing Provider"""

    name: str = config.SCRUB_PROVIDER_NAME[0]
    capabilities: List[Modality] = [Modality.TEXT, Modality.IMAGE]

    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        """Scrub the text of all PII/PHI using Presidio ANALYZER.TRF and Anonymizer.

        Args:
            text (str): Text to be scrubbed
            is_separated (bool): Whether the text is separated with special characters

        Returns:
            str: Scrubbed text
        """
        if text is None:
            return None

        SCRUB_PROVIDER_TRF = NlpEngineProvider(
            nlp_configuration=config.SCRUB_CONFIG_TRF
        )
        NLP_ENGINE_TRF = SCRUB_PROVIDER_TRF.create_engine()
        ANALYZER_TRF = AnalyzerEngine(
            nlp_engine=NLP_ENGINE_TRF, supported_languages=["en"]
        )
        ANONYMIZER = AnonymizerEngine()
        SCRUBBING_ENTITIES = [
            entity
            for entity in ANALYZER_TRF.get_supported_entities()
            if entity not in config.SCRUB_IGNORE_ENTITIES
        ]

        if is_separated and not (
            text.startswith(config.ACTION_TEXT_NAME_PREFIX)
            or text.endswith(config.ACTION_TEXT_NAME_SUFFIX)
        ):
            text = "".join(text.split(config.ACTION_TEXT_SEP))

        # Access ANALYZER_TRF as a class attribute
        analyzer_results = ANALYZER_TRF.analyze(
            text=text,
            entities=SCRUBBING_ENTITIES,
            language=config.SCRUB_LANGUAGE,
        )

        logger.debug(f"analyzer_results: {analyzer_results}")

        anonymized_results = ANONYMIZER.anonymize(
            text=text,
            analyzer_results=analyzer_results,
        )

        logger.debug(f"anonymized_results: {anonymized_results}")

        if is_separated and not (
            text.startswith(config.ACTION_TEXT_NAME_PREFIX)
            or text.endswith(config.ACTION_TEXT_NAME_SUFFIX)
        ):
            anonymized_results.text = config.ACTION_TEXT_SEP.join(
                anonymized_results.text
            )

        return anonymized_results.text

    def scrub_image(
        self, image: Image, fill_color: int = config.SCRUB_FILL_COLOR
    ) -> Image:
        """Scrub the image of all PII/PHI using Presidio Image Redactor.

        Args:
            image (Image): A PIL.Image object to be scrubbed
            fill_color (int): The color used to fill the redacted regions(BGR).

        Returns:
            Image: The scrubbed image with PII and PHI removed.
        """
        SCRUB_PROVIDER_TRF = NlpEngineProvider(
            nlp_configuration=config.SCRUB_CONFIG_TRF
        )
        NLP_ENGINE_TRF = SCRUB_PROVIDER_TRF.create_engine()
        ANALYZER_TRF = AnalyzerEngine(
            nlp_engine=NLP_ENGINE_TRF, supported_languages=["en"]
        )
        IMAGE_REDACTOR = ImageRedactorEngine(ImageAnalyzerEngine(ANALYZER_TRF))
        SCRUBBING_ENTITIES = [
            entity
            for entity in ANALYZER_TRF.get_supported_entities()
            if entity not in config.SCRUB_IGNORE_ENTITIES
        ]

        redacted_image = IMAGE_REDACTOR.redact(
            image, fill=fill_color, entities=SCRUBBING_ENTITIES
        )

        return redacted_image

    def scrub_text_all(self, text: str) -> str:
        """Scrub the text by replacing all characters with config.SCRUB_CHAR.

        Args:
            text (str): Text to be scrubbed

        Returns:
            str: Scrubbed text
        """
        return config.SCRUB_CHAR * len(text)

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
            list_keys = config.SCRUB_KEYS_HTML

        scrubbed_dict = {}
        for key, value in input_dict.items():
            if _should_scrub_text(key, value, list_keys, scrub_all):
                scrubbed_text = _scrub_text_item(value, key, force_scrub_children)
                if key in ("text", "canonical_text") and _is_scrubbed(
                    value, scrubbed_text
                ):
                    force_scrub_children = True
                scrubbed_dict[key] = scrubbed_text
            elif isinstance(value, list):
                scrubbed_list = [
                    (
                        _scrub_list_item(item, key, list_keys, force_scrub_children)
                        if _should_scrub_list_item(item, key, list_keys)
                        else item
                    )
                    for item in value
                ]
                scrubbed_dict[key] = scrubbed_list
                force_scrub_children = False
            elif isinstance(value, dict):
                if isinstance(key, str) and key == "state":
                    scrubbed_dict[key] = scrub_dict(value, list_keys, scrub_all=True)
                else:
                    scrubbed_dict[key] = scrub_dict(value, list_keys)
            else:
                scrubbed_dict[key] = value

        return scrubbed_dict

    def scrub_list_dicts(
        self, input_list: list[dict], list_keys: list = None
    ) -> list[dict]:
        """Scrub list of dicts to remove PII/PHI
        using Presidio ANALYZER.TRF and Anonymizer.

        Args:
            input_list (list[dict]): A list of dicts to be scrubbed
            list_keys (list): List of keys to be scrubbed

        Returns:
            list[dict]: The scrubbed list of dicts with PII and PHI removed.
        """
        scrubbed_list_dicts = []
        for input_dict in input_list:
            scrubbed_list_dicts.append(scrub_dict(input_dict, list_keys))

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
            return scrub_text(value, is_separated=True)
        if force_scrub_children:
            return scrub_text_all(value)
        return scrub_text(value)

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
            return scrub_dict(
                item, list_keys, force_scrub_children=force_scrub_children
            )
        return _scrub_text_item(
            item, key
        )  # noqa: F821 & pylint: disable=undefined-variable
