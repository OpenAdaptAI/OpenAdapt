from typing import Union

from loguru import logger
from PIL import Image
from presidio_analyzer import AnalyzerEngine
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_anonymizer import AnonymizerEngine
from presidio_image_redactor import ImageAnalyzerEngine, ImageRedactorEngine
import fire

from openadapt import config, utils
from openadapt.privacy.base import Modality, ScrubbingProvider


class ScrubbingProviderFactory:
    """A Factory Class for Scrubbing Providers"""

    @staticmethod
    def get_for_modality(modality: Modality) -> list[ScrubbingProvider]:
        scrubbing_providers = ScrubbingProvider.__subclasses__()
        filtered_providers = [
            provider()
            for provider in scrubbing_providers
            if modality in provider().Capabilities
        ]
        return filtered_providers


class PresidioScrubbingProvider(ScrubbingProvider):
    """A Class for Presidio Scrubbing Provider"""

    self.name = "Presidio"
    self.capabilities = [Modality.TEXT]

    SCRUB_PROVIDER_TRF = NlpEngineProvider(nlp_configuration=config.SCRUB_CONFIG_TRF)
    NLP_ENGINE_TRF = SCRUB_PROVIDER_TRF.create_engine()
    ANALYZER_TRF = AnalyzerEngine(nlp_engine=NLP_ENGINE_TRF, supported_languages=["en"])
    ANONYMIZER = AnonymizerEngine()
    IMAGE_REDACTOR = ImageRedactorEngine(ImageAnalyzerEngine(ANALYZER_TRF))
    SCRUBBING_ENTITIES = [
        entity
        for entity in ANALYZER_TRF.get_supported_entities()
        if entity not in config.SCRUB_IGNORE_ENTITIES
    ]

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

        if is_separated and not (
            text.startswith(config.ACTION_TEXT_NAME_PREFIX)
            or text.endswith(config.ACTION_TEXT_NAME_SUFFIX)
        ):
            text = "".join(text.split(config.ACTION_TEXT_SEP))

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
