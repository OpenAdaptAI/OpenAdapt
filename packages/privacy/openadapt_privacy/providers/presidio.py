"""Presidio-based scrubbing provider.

This module implements PII/PHI scrubbing using Microsoft Presidio with an
allowlisted local spaCy pipeline. It never downloads a model or loads an
unapproved operator-selected model at runtime.
"""

from __future__ import annotations

import logging
import warnings
from typing import List

from PIL import Image

from openadapt_privacy.base import Modality, ScrubbingProvider, TextScrubbingMixin
from openadapt_privacy.config import config

logger = logging.getLogger(__name__)

SUPPORTED_SPACY_MODELS = {"en": frozenset({"en_core_web_sm"})}
_UI_COMMAND_PREFIXES = frozenset(
    {
        "cancel",
        "choose",
        "click",
        "close",
        "enter",
        "open",
        "press",
        "retry",
        "save",
        "select",
        "submit",
        "type",
    }
)


class PrivacyModelUnavailable(RuntimeError):
    """The required, allowlisted local NLP model is unavailable or invalid."""


# Lazy-loaded Presidio components
_analyzer_engine = None
_anonymizer_engine = None
_image_redactor_engine = None
_scrubbing_entities = None


def _ensure_spacy_model() -> None:
    """Validate the configured local model without downloading any code."""
    import spacy

    allowed_models = SUPPORTED_SPACY_MODELS.get(config.SCRUB_LANGUAGE)
    if allowed_models is None:
        raise PrivacyModelUnavailable(
            f"Refusing unsupported scrub language {config.SCRUB_LANGUAGE!r}; "
            f"allowed languages: {sorted(SUPPORTED_SPACY_MODELS)}"
        )
    expected_config = {
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": config.SCRUB_LANGUAGE, "model_name": config.SPACY_MODEL_NAME}],
    }
    if config.SPACY_MODEL_NAME not in allowed_models:
        raise PrivacyModelUnavailable(
            f"Refusing unapproved spaCy model {config.SPACY_MODEL_NAME!r}; "
            f"allowed models for {config.SCRUB_LANGUAGE!r}: {sorted(allowed_models)}"
        )
    if config.SCRUB_CONFIG_TRF != expected_config:
        raise PrivacyModelUnavailable(
            "Refusing inconsistent Presidio NLP configuration; model selection "
            "must match the allowlisted local SPACY_MODEL_NAME"
        )
    if not spacy.util.is_package(config.SPACY_MODEL_NAME):
        raise PrivacyModelUnavailable(
            f"Required spaCy model {config.SPACY_MODEL_NAME!r} is not installed. "
            f"Install it explicitly with: python -m spacy download "
            f"{config.SPACY_MODEL_NAME}. No scrub was attempted."
        )


def _register_phi_recognizers(analyzer_engine) -> None:
    """Add context-bound identifiers absent from Presidio's generic registry."""
    from presidio_analyzer import Pattern, PatternRecognizer

    identifier = r"[A-Z0-9](?:[A-Z0-9-]{4,30}[A-Z0-9])?"
    medical_patterns = [
        Pattern("mrn", rf"(?i)(?<=MRN:\s){identifier}", 0.9),
        Pattern("medical-record-number", rf"(?i)(?<=Medical record number\s){identifier}", 0.9),
        Pattern("patient-id", rf"(?i)(?<=Patient ID:\s){identifier}", 0.9),
        Pattern("member-id", rf"(?i)(?<=member ID\s){identifier}", 0.9),
    ]
    street = (
        r"\d{1,6}\s+(?:[A-Za-z0-9.'-]+\s+){0,6}"
        r"(?:Street|St|Road|Rd|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Lane|Ln)\b"
        r"(?:\s+[NSEW])?"
    )
    address_patterns = [
        Pattern("home-address", rf"(?i)(?<=Home address:\s){street}", 0.85),
        Pattern("mail-address", rf"(?i)(?<=Mail to\s){street}", 0.85),
    ]
    analyzer_engine.registry.add_recognizer(
        PatternRecognizer(supported_entity="MEDICAL_RECORD_NUMBER", patterns=medical_patterns)
    )
    analyzer_engine.registry.add_recognizer(
        PatternRecognizer(supported_entity="STREET_ADDRESS", patterns=address_patterns)
    )


def _filter_automation_false_positives(text: str, analyzer_results: list) -> list:
    """Drop NER findings that are clearly GUI imperatives, not identifiers."""
    filtered = []
    for result in analyzer_results:
        candidate = text[result.start : result.end].strip().lower()
        prefix = text[: result.start].strip().lower()
        first_word = candidate.split(maxsplit=1)[0] if candidate else ""
        if (
            result.entity_type in {"PERSON", "ORGANIZATION"}
            and prefix in {"", "please"}
            and first_word in _UI_COMMAND_PREFIXES
        ):
            continue
        filtered.append(result)
    return filtered


def _get_analyzer_engine():
    """Get or create the Presidio analyzer engine (lazy initialization)."""
    global _analyzer_engine, _scrubbing_entities

    # Revalidate on every access so a cached analyzer cannot mask a later,
    # operator-controlled configuration change.
    _ensure_spacy_model()
    if _analyzer_engine is None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from presidio_analyzer import AnalyzerEngine
            from presidio_analyzer.nlp_engine import NlpEngineProvider

        nlp_provider = NlpEngineProvider(nlp_configuration=config.SCRUB_CONFIG_TRF)
        nlp_engine = nlp_provider.create_engine()
        _analyzer_engine = AnalyzerEngine(
            nlp_engine=nlp_engine,
            supported_languages=[config.SCRUB_LANGUAGE],
        )
        _register_phi_recognizers(_analyzer_engine)

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

    Uses Presidio Analyzer with an allowlisted local spaCy model and
    Presidio Anonymizer/Image Redactor for scrubbing.
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
        analyzer_results = _filter_automation_false_positives(text, analyzer_results)

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
