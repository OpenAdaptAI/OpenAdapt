"""Presidio-based scrubbing provider.

This module implements PII/PHI scrubbing using Microsoft Presidio with an
allowlisted local spaCy pipeline. It never downloads a model or loads an
unapproved operator-selected model at runtime.
"""

from __future__ import annotations

import logging
import threading
import warnings
from typing import List

from PIL import Image

from openadapt_privacy.base import Modality, ScrubbingProvider, TextScrubbingMixin
from openadapt_privacy.config import effective_config

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

# Entity types that reach us from the spaCy pipeline. Their spans are model
# output: the model picks the boundary, so a span that overlaps a deterministic
# identifier match is the model reading an identifier plus the words near it.
_STATISTICAL_ENTITIES = frozenset(
    {
        "DATE_TIME",
        "LOCATION",
        "NRP",
        "ORGANIZATION",
        "PERSON",
    }
)

# Presidio scores an unvalidated, context-free digit run at 0.01-0.05. Below
# this floor a pattern recognizer is guessing too, and must not take a label
# away from the NER result.
_MIN_DETERMINISTIC_SCORE = 0.4


class PrivacyModelUnavailable(RuntimeError):
    """The required, allowlisted local NLP model is unavailable or invalid."""


# Lazy-loaded Presidio components
_analyzer_engine = None
_anonymizer_engine = None
_image_redactor_engine = None
_scrubbing_entities = None
_analyzer_policy_sha256 = None
_cache_lock = threading.RLock()


def _invalidate_policy_bound_caches(current_policy_sha256: str) -> None:
    """Discard analyzer-derived state when the effective policy changes."""
    global _analyzer_engine, _analyzer_policy_sha256, _image_redactor_engine
    global _scrubbing_entities

    if _analyzer_policy_sha256 == current_policy_sha256:
        return
    _analyzer_engine = None
    _image_redactor_engine = None
    _scrubbing_entities = None
    _analyzer_policy_sha256 = current_policy_sha256


def _ensure_spacy_model() -> None:
    """Validate the configured local model without downloading any code."""
    import spacy

    policy = effective_config()
    allowed_models = SUPPORTED_SPACY_MODELS.get(policy.SCRUB_LANGUAGE)
    if allowed_models is None:
        raise PrivacyModelUnavailable(
            f"Refusing unsupported scrub language {policy.SCRUB_LANGUAGE!r}; "
            f"allowed languages: {sorted(SUPPORTED_SPACY_MODELS)}"
        )
    expected_config = {
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": policy.SCRUB_LANGUAGE, "model_name": policy.SPACY_MODEL_NAME}],
    }
    if policy.SPACY_MODEL_NAME not in allowed_models:
        raise PrivacyModelUnavailable(
            f"Refusing unapproved spaCy model {policy.SPACY_MODEL_NAME!r}; "
            f"allowed models for {policy.SCRUB_LANGUAGE!r}: {sorted(allowed_models)}"
        )
    if policy.SCRUB_CONFIG_TRF != expected_config:
        raise PrivacyModelUnavailable(
            "Refusing inconsistent Presidio NLP configuration; model selection "
            "must match the allowlisted local SPACY_MODEL_NAME"
        )
    if not spacy.util.is_package(policy.SPACY_MODEL_NAME):
        raise PrivacyModelUnavailable(
            f"Required spaCy model {policy.SPACY_MODEL_NAME!r} is not installed. "
            f"Install it explicitly with: python -m spacy download "
            f"{policy.SPACY_MODEL_NAME}. No scrub was attempted."
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


def _covers_every_digit(text: str, result, deterministic: list) -> bool:
    """Report whether pattern matches claim every digit inside `result`'s span.

    Returns False when nothing overlaps, so a statistical result that stands
    alone is never touched.
    """
    covered: set[int] = set()
    overlaps = False
    for other in deterministic:
        if result.start < other.end and other.start < result.end:
            overlaps = True
            covered.update(range(max(result.start, other.start), min(result.end, other.end)))
    if not overlaps:
        return False
    return not any(
        text[index].isdigit() for index in range(result.start, result.end) if index not in covered
    )


def _prefer_deterministic_identifier_labels(text: str, analyzer_results: list) -> list:
    """Give an overlapped span the label of the recognizer that validated it.

    Presidio's anonymizer settles an overlap by span before score, so a wide
    spaCy `DATE_TIME` at 0.85 replaced a Luhn-checked `CREDIT_CARD` at 1.0 and
    the output named the wrong entity type. A card number that reads as
    `<DATE_TIME>` is still redacted, but no policy can route on it.

    The statistical result is dropped only when the pattern matches cover every
    digit it claimed, so this can never expose part of an identifier: where the
    NER span reaches past the pattern match into more digits, the wider span
    stays.
    """
    deterministic = [
        result
        for result in analyzer_results
        if result.entity_type not in _STATISTICAL_ENTITIES
        and result.score >= _MIN_DETERMINISTIC_SCORE
    ]
    if not deterministic:
        return analyzer_results

    return [
        result
        for result in analyzer_results
        if not (
            result.entity_type in _STATISTICAL_ENTITIES
            and _covers_every_digit(text, result, deterministic)
        )
    ]


def _is_separated_form(text: str, separator: str) -> bool:
    """Report whether `text` is a key sequence rather than prose.

    Action text is one typed character per separator: `ACTION_TEXT_SEP.join(
    "john")` gives `j-o-h-n`. Prose that holds a separator ("follow-up",
    "555-123-4567") has chunks longer than one character, so it is prose.
    """
    if not separator or separator not in text:
        return False
    chunks = text.split(separator)
    return len(chunks) >= 2 and all(len(chunk) <= 1 for chunk in chunks)


def _get_analyzer_engine():
    """Get or create the Presidio analyzer engine (lazy initialization)."""
    global _analyzer_engine, _scrubbing_entities

    with _cache_lock:
        # Revalidate on every access so a cached analyzer cannot mask a later,
        # operator-controlled configuration change.
        _ensure_spacy_model()
        policy = effective_config()
        _invalidate_policy_bound_caches(policy.policy_digest())
        if _analyzer_engine is None:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                from presidio_analyzer import AnalyzerEngine
                from presidio_analyzer.nlp_engine import NlpEngineProvider

            nlp_provider = NlpEngineProvider(nlp_configuration=policy.SCRUB_CONFIG_TRF)
            nlp_engine = nlp_provider.create_engine()
            _analyzer_engine = AnalyzerEngine(
                nlp_engine=nlp_engine,
                supported_languages=[policy.SCRUB_LANGUAGE],
            )
            _register_phi_recognizers(_analyzer_engine)

            # Cache the scrubbing entities
            _scrubbing_entities = [
                entity
                for entity in _analyzer_engine.get_supported_entities()
                if entity not in policy.SCRUB_PRESIDIO_IGNORE_ENTITIES
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

    with _cache_lock:
        analyzer = _get_analyzer_engine()
        if _image_redactor_engine is None:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                from presidio_image_redactor import ImageAnalyzerEngine, ImageRedactorEngine

            _image_redactor_engine = ImageRedactorEngine(ImageAnalyzerEngine(analyzer))

        return _image_redactor_engine


def _get_scrubbing_entities() -> List[str]:
    """Get the list of entity types to scrub."""
    global _scrubbing_entities

    with _cache_lock:
        _get_analyzer_engine()
        return list(_scrubbing_entities)


class PresidioScrubbingProvider(ScrubbingProvider, TextScrubbingMixin):
    """Scrubbing provider using Microsoft Presidio.

    Uses Presidio Analyzer with an allowlisted local spaCy model and
    Presidio Anonymizer/Image Redactor for scrubbing.
    """

    name: str = "PRESIDIO"
    capabilities: List[str] = [Modality.TEXT, Modality.PIL_IMAGE]

    def validate_ready(self, modalities: list[str]) -> None:
        """Validate every local dependency before any source content is read."""
        super().validate_ready(modalities)
        _ensure_spacy_model()
        try:
            import presidio_analyzer  # noqa: F401
            import presidio_anonymizer  # noqa: F401

            if Modality.PIL_IMAGE in modalities:
                import presidio_image_redactor  # noqa: F401
        except ImportError as exc:
            raise PrivacyModelUnavailable(
                "The configured Presidio scrubber is incomplete; no scrub was attempted. "
                "Install it with: pip install 'openadapt-privacy[presidio]'"
            ) from exc

    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        """Scrub PII/PHI from text using Presidio.

        Args:
            text: Text to be scrubbed.
            is_separated: Permit key-sequence handling (e.g. "a-b-c"). The
                text is reassembled and re-separated only when it is actually
                in that form; prose passes through unchanged, because splitting
                prose one character at a time destroys it.

        Returns:
            Scrubbed text with PII/PHI replaced by entity type placeholders.
        """
        if text is None:
            return None

        policy = effective_config()
        analyzer = _get_analyzer_engine()
        anonymizer = _get_anonymizer_engine()
        entities = _get_scrubbing_entities()

        # Handle separated text (e.g., key sequences)
        separated = (
            is_separated
            and not (
                text.startswith(policy.ACTION_TEXT_NAME_PREFIX)
                or text.endswith(policy.ACTION_TEXT_NAME_SUFFIX)
            )
            and _is_separated_form(text, policy.ACTION_TEXT_SEP)
        )
        if separated:
            text = "".join(text.split(policy.ACTION_TEXT_SEP))

        # Analyze and anonymize
        analyzer_results = analyzer.analyze(
            text=text,
            entities=entities,
            language=policy.SCRUB_LANGUAGE,
        )
        analyzer_results = _filter_automation_false_positives(text, analyzer_results)
        analyzer_results = _prefer_deterministic_identifier_labels(text, analyzer_results)

        logger.debug(f"analyzer_results: {analyzer_results}")

        anonymized_results = anonymizer.anonymize(
            text=text,
            analyzer_results=analyzer_results,
        )

        logger.debug(f"anonymized_results: {anonymized_results}")

        result_text = anonymized_results.text

        # Restore separator format if needed
        if separated:
            result_text = policy.ACTION_TEXT_SEP.join(result_text)

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
            fill_color = effective_config().SCRUB_FILL_COLOR

        redactor = _get_image_redactor_engine()
        entities = _get_scrubbing_entities()

        redacted_image = redactor.redact(
            image,
            fill=fill_color,
            entities=entities,
        )

        return redacted_image
