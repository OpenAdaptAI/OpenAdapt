"""Token-based secret field-name matching shared by the extension layers.

A field identifier (``name``, ``id``, ``aria-label``, ``autocomplete`` or a
JSON key) is split into lowercase tokens, then matched as whole consecutive
tokens against ``secret_field_vocabulary.json``. So ``patientSsn``,
``PatientSSN``, ``card_cvv``, ``card-cvv``, ``card.cvv`` and "Card CVV" all
match, while ``pinnedItems``, ``spinnerState`` and ``opinionText`` do not.

``chrome_extension/content.js`` and ``chrome_extension/network_redact.js``
implement the same ``fieldTokens`` split and embed the same phrase list; keep
the three in step (a test compares the lists).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Final

_VOCABULARY_PATH: Final = Path(__file__).with_name("secret_field_vocabulary.json")

SECRET_FIELD_PHRASES: Final[tuple[str, ...]] = tuple(
    json.loads(_VOCABULARY_PATH.read_text(encoding="utf-8"))["phrases"]
)
_PHRASE_TOKENS: Final[tuple[tuple[str, ...], ...]] = tuple(
    tuple(phrase.split()) for phrase in SECRET_FIELD_PHRASES
)

_LOWER_UPPER = re.compile(r"([a-z0-9])([A-Z])")
_ACRONYM_WORD = re.compile(r"([A-Z]+)([A-Z][a-z])")
_SEPARATORS = re.compile(r"[^a-z0-9]+")


def field_tokens(value: object) -> list[str]:
    """Lowercase tokens of an identifier: ``"PatientSSNLast4"`` -> patient ssn last4."""

    text = _LOWER_UPPER.sub(r"\1 \2", str(value or ""))
    text = _ACRONYM_WORD.sub(r"\1 \2", text).lower()
    return [token for token in _SEPARATORS.split(text) if token]


def names_secret_field(value: object) -> bool:
    """True when the identifier contains a secret phrase as whole tokens."""

    tokens = field_tokens(value)
    for phrase in _PHRASE_TOKENS:
        width = len(phrase)
        for start in range(len(tokens) - width + 1):
            if tuple(tokens[start : start + width]) == phrase:
                return True
    return False
