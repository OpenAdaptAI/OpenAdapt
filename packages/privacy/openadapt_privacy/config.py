"""Configuration for privacy scrubbing.

This module provides configuration settings for PII/PHI scrubbing operations.
Settings can be customized by creating a new PrivacyConfig instance or modifying
the global `config` instance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence


@dataclass
class PrivacyConfig:
    """Configuration for privacy scrubbing operations.

    Attributes:
        SCRUB_CHAR: Character used to replace scrubbed text when using scrub_text_all.
        SCRUB_LANGUAGE: Language code for NLP analysis (default: "en").
        SCRUB_FILL_COLOR: BGR color value for image redaction (default: blue 0x0000FF).
        SCRUB_KEYS_HTML: List of dict keys that should be scrubbed.
        ACTION_TEXT_NAME_PREFIX: Prefix for action text names (e.g., "<").
        ACTION_TEXT_NAME_SUFFIX: Suffix for action text names (e.g., ">").
        ACTION_TEXT_SEP: Separator for action text sequences (e.g., "-").
        SCRUB_CONFIG_TRF: Presidio NLP engine configuration.
        SCRUB_PRESIDIO_IGNORE_ENTITIES: Entity types to ignore during scrubbing.
        SPACY_MODEL_NAME: Name of the spaCy model to use.
    """

    # Character used to replace scrubbed text
    SCRUB_CHAR: str = "*"

    # Language for NLP analysis
    SCRUB_LANGUAGE: str = "en"

    # BGR color for image redaction (blue by default)
    SCRUB_FILL_COLOR: int = 0x0000FF

    # Keys in dicts that should be scrubbed
    SCRUB_KEYS_HTML: list[str] = field(
        default_factory=lambda: [
            "text",
            "canonical_text",
            "title",
            "state",
            "task_description",
            "key_char",
            "canonical_key_char",
            "key_vk",
            "children",
            "value",
            "tooltip",
        ]
    )

    # Action text formatting (for handling separated text like key sequences)
    ACTION_TEXT_NAME_PREFIX: str = "<"
    ACTION_TEXT_NAME_SUFFIX: str = ">"
    ACTION_TEXT_SEP: str = "-"

    # Presidio NLP engine configuration
    SCRUB_CONFIG_TRF: dict = field(
        default_factory=lambda: {
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": "en_core_web_trf"}],
        }
    )

    # Entity types to ignore during Presidio scrubbing
    SCRUB_PRESIDIO_IGNORE_ENTITIES: Sequence[str] = field(default_factory=list)

    # SpaCy model name
    SPACY_MODEL_NAME: str = "en_core_web_trf"


# Global default configuration instance
config = PrivacyConfig()
