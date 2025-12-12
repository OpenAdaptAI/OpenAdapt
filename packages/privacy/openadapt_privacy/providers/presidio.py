"""Presidio-based scrubbing provider.

This module implements PII/PHI scrubbing using Microsoft Presidio with
spaCy transformer models for NER.
"""

from __future__ import annotations

import logging
import warnings
from typing import List

from PIL import Image

from openadapt_privacy.base import Modality, ScrubbingProvider, TextScrubbingMixin
from openadapt_privacy.config import config

logger = logging.getLogger(__name__)

# Lazy-loaded Presidio components
_analyzer_engine = None
_anonymizer_engine = None
_image_redactor_engine = None
_scrubbing_entities = None


def _ensure_spacy_model() -> None:
    """Ensure the spaCy model is downloaded."""
    import spacy

    if not spacy.util.is_package(config.SPACY_MODEL_NAME):
        logger.info(f"Downloading {config.SPACY_MODEL_NAME} model...")
        spacy.cli.download(config.SPACY_MODEL_NAME)


def _get_analyzer_engine():
    """Get or create the Presidio analyzer engine (lazy initialization)."""
    global _analyzer_engine, _scrubbing_entities

    if _analyzer_engine is None:
        _ensure_spacy_model()

        # Import spacy_transformers to register the transformer pipeline
        import spacy_transformers  # noqa: F401

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from presidio_analyzer import AnalyzerEngine
            from presidio_analyzer.nlp_engine import NlpEngineProvider

        nlp_provider = NlpEngineProvider(nlp_configuration=config.SCRUB_CONFIG_TRF)
        nlp_engine = nlp_provider.create_engine()
        _analyzer_engine = AnalyzerEngine(
            nlp_engine=nlp_engine, supported_languages=["en"]
        )

        # Cache the scrubbing entities
        _scrubbing_entities = [
            entity
            for entity in _analyzer_engine.get_supported_entities()
            if entity not in config.SCRUB_PRESIDIO_IGNORE_ENTITIES
        ]

    return _analyzer_engine


def _get_anonymizer_engine():
    """Get or create the Presidio anonymizer engine (lazy initialization)."""
    global _anonymizer_engine

    if _anonymizer_engine is None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from presidio_anonymizer import AnonymizerEngine

        _anonymizer_engine = AnonymizerEngine()

    return _anonymizer_engine


def _get_image_redactor_engine():
    """Get or create the Presidio image redactor engine (lazy initialization)."""
    global _image_redactor_engine

    if _image_redactor_engine is None:
        analyzer = _get_analyzer_engine()

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from presidio_image_redactor import ImageAnalyzerEngine, ImageRedactorEngine

        _image_redactor_engine = ImageRedactorEngine(ImageAnalyzerEngine(analyzer))

    return _image_redactor_engine


def _get_scrubbing_entities() -> List[str]:
    """Get the list of entity types to scrub."""
    global _scrubbing_entities

    if _scrubbing_entities is None:
        _get_analyzer_engine()  # This will populate _scrubbing_entities

    return _scrubbing_entities


class PresidioScrubbingProvider(ScrubbingProvider, TextScrubbingMixin):
    """Scrubbing provider using Microsoft Presidio.

    Uses Presidio Analyzer with spaCy transformer models for named entity
    recognition and Presidio Anonymizer/Image Redactor for scrubbing.
    """

    name: str = "PRESIDIO"
    capabilities: List[str] = [Modality.TEXT, Modality.PIL_IMAGE]

    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        """Scrub PII/PHI from text using Presidio.

        Args:
            text: Text to be scrubbed.
            is_separated: Whether the text contains separated characters
                (e.g., key sequences like "a-b-c").

        Returns:
            Scrubbed text with PII/PHI replaced by entity type placeholders.
        """
        if text is None:
            return None

        analyzer = _get_analyzer_engine()
        anonymizer = _get_anonymizer_engine()
        entities = _get_scrubbing_entities()

        # Handle separated text (e.g., key sequences)
        original_text = text
        if is_separated and not (
            text.startswith(config.ACTION_TEXT_NAME_PREFIX)
            or text.endswith(config.ACTION_TEXT_NAME_SUFFIX)
        ):
            text = "".join(text.split(config.ACTION_TEXT_SEP))

        # Analyze and anonymize
        analyzer_results = analyzer.analyze(
            text=text,
            entities=entities,
            language=config.SCRUB_LANGUAGE,
        )

        logger.debug(f"analyzer_results: {analyzer_results}")

        anonymized_results = anonymizer.anonymize(
            text=text,
            analyzer_results=analyzer_results,
        )

        logger.debug(f"anonymized_results: {anonymized_results}")

        result_text = anonymized_results.text

        # Restore separator format if needed
        if is_separated and not (
            original_text.startswith(config.ACTION_TEXT_NAME_PREFIX)
            or original_text.endswith(config.ACTION_TEXT_NAME_SUFFIX)
        ):
            result_text = config.ACTION_TEXT_SEP.join(result_text)

        return result_text

    def scrub_image(
        self,
        image: Image.Image,
        fill_color: int | None = None,
    ) -> Image.Image:
        """Scrub PII/PHI from an image using Presidio Image Redactor.

        Args:
            image: PIL Image object to be scrubbed.
            fill_color: BGR color value for redacted regions.
                Defaults to config.SCRUB_FILL_COLOR.

        Returns:
            Scrubbed image with PII/PHI redacted.
        """
        if fill_color is None:
            fill_color = config.SCRUB_FILL_COLOR

        redactor = _get_image_redactor_engine()
        entities = _get_scrubbing_entities()

        redacted_image = redactor.redact(
            image,
            fill=fill_color,
            entities=entities,
        )

        return redacted_image
