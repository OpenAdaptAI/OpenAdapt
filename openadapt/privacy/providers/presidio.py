"""A Module for Presidio Scrubbing Provider Class."""

from typing import List

from loguru import logger
from PIL import Image

from openadapt.build_utils import redirect_stdout_stderr

with redirect_stdout_stderr():
    from presidio_analyzer import AnalyzerEngine
    from presidio_analyzer.nlp_engine import NlpEngineProvider
    from presidio_anonymizer import AnonymizerEngine
    from presidio_image_redactor import ImageAnalyzerEngine, ImageRedactorEngine

import spacy
import spacy_transformers  # pylint: disable=unused-import # noqa: F401

from openadapt.build_utils import is_running_from_executable
from openadapt.config import config
from openadapt.privacy.base import Modality, ScrubbingProvider, TextScrubbingMixin
from openadapt.privacy.providers import ScrubProvider

if not spacy.util.is_package(config.SPACY_MODEL_NAME):  # pylint: disable=no-member
    if not is_running_from_executable():
        logger.info(f"Downloading {config.SPACY_MODEL_NAME} model...")
        spacy.cli.download(config.SPACY_MODEL_NAME)
    else:
        # TODO: devise some method to download this automatically
        # this is running from inside a pyinstaller build
        logger.warning(f"Download {config.SPACY_MODEL_NAME} model manually.")


class PresidioScrubbingProvider(
    ScrubProvider, ScrubbingProvider, TextScrubbingMixin
):  # pylint: disable=W0223
    """A Class for Presidio Scrubbing Provider."""

    name: str = ScrubProvider.PRESIDIO  # pylint: disable=E1101
    capabilities: List[Modality] = [Modality.TEXT, Modality.PIL_IMAGE]

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

        SCRUB_PROVIDER_TRF = NlpEngineProvider(  # pylint: disable=C0103
            nlp_configuration=config.SCRUB_CONFIG_TRF  # pylint: disable=E1101
        )
        NLP_ENGINE_TRF = SCRUB_PROVIDER_TRF.create_engine()  # pylint: disable=C0103
        ANALYZER_TRF = AnalyzerEngine(  # pylint: disable=C0103
            nlp_engine=NLP_ENGINE_TRF, supported_languages=["en"]
        )
        ANONYMIZER = AnonymizerEngine()  # pylint: disable=C0103
        SCRUBBING_ENTITIES = [  # pylint: disable=C0103
            entity
            for entity in ANALYZER_TRF.get_supported_entities()
            if entity
            not in config.SCRUB_PRESIDIO_IGNORE_ENTITIES  # pylint: disable=E1101
        ]

        if is_separated and not (
            text.startswith(config.ACTION_TEXT_NAME_PREFIX)  # pylint: disable=E1101
            or text.endswith(config.ACTION_TEXT_NAME_SUFFIX)  # pylint: disable=E1101
        ):
            text = "".join(text.split(config.ACTION_TEXT_SEP))  # pylint: disable=E1101

        # Access ANALYZER_TRF as a class attribute
        analyzer_results = ANALYZER_TRF.analyze(
            text=text,
            entities=SCRUBBING_ENTITIES,
            language=config.SCRUB_LANGUAGE,  # pylint: disable=E1101
        )

        logger.debug(f"analyzer_results: {analyzer_results}")

        anonymized_results = ANONYMIZER.anonymize(
            text=text,
            analyzer_results=analyzer_results,
        )

        logger.debug(f"anonymized_results: {anonymized_results}")

        if is_separated and not (
            text.startswith(config.ACTION_TEXT_NAME_PREFIX)  # pylint: disable=E1101
            or text.endswith(config.ACTION_TEXT_NAME_SUFFIX)  # pylint: disable=E1101
        ):
            anonymized_results.text = (
                config.ACTION_TEXT_SEP.join(  # pylint: disable=E1101
                    anonymized_results.text
                )
            )

        return anonymized_results.text

    def scrub_image(
        self,
        image: Image,
        fill_color: int = config.SCRUB_FILL_COLOR,  # pylint: disable=E1101
    ) -> Image:
        """Scrub the image of all PII/PHI using Presidio Image Redactor.

        Args:
            image (Image): A PIL.Image object to be scrubbed
            fill_color (int): The color used to fill the redacted regions(BGR).

        Returns:
            Image: The scrubbed image with PII and PHI removed.
        """
        SCRUB_PROVIDER_TRF = NlpEngineProvider(  # pylint: disable=C0103
            nlp_configuration=config.SCRUB_CONFIG_TRF  # pylint: disable=E1101
        )
        NLP_ENGINE_TRF = SCRUB_PROVIDER_TRF.create_engine()  # pylint: disable=C0103
        ANALYZER_TRF = AnalyzerEngine(  # pylint: disable=C0103
            nlp_engine=NLP_ENGINE_TRF, supported_languages=["en"]
        )
        IMAGE_REDACTOR = ImageRedactorEngine(  # pylint: disable=C0103
            ImageAnalyzerEngine(ANALYZER_TRF)
        )  # pylint: disable=C0103
        SCRUBBING_ENTITIES = [  # pylint: disable=C0103
            entity
            for entity in ANALYZER_TRF.get_supported_entities()
            if entity
            not in config.SCRUB_PRESIDIO_IGNORE_ENTITIES  # pylint: disable=E1101
        ]

        redacted_image = IMAGE_REDACTOR.redact(
            image, fill=fill_color, entities=SCRUBBING_ENTITIES
        )

        return redacted_image
