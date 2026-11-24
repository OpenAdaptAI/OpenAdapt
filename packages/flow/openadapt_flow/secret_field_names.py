"""Token-based secret field-name matching shared by the extension layers.

A field identifier (``name``, ``id``, ``aria-label``, ``autocomplete`` or a
JSON key) is split into lowercase tokens, then matched as whole consecutive
tokens against ``secret_field_vocabulary.json``. So ``patientSsn``,
``PatientSSN``, ``card_cvv``, ``card-cvv``, ``card.cvv`` and "Card CVV" all
match, while ``pinnedItems``, ``spinnerState`` and ``opinionText`` do not.
An identifier whose whole token list is one of ``exact_names`` (``code``,
``sig``, ``sid``, ``refresh``) is secret too, so ``code`` and ``Sig`` match
but ``productCode`` and ``refresh_interval`` do not. An identifier whose
last token is one of ``suffix_names`` (``session``, ``authorization``) is
secret, so ``sso_session`` and ``proxy-authorization`` match but
``session_count`` and ``authorization_id`` do not.

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

_VOCABULARY: Final = json.loads(_VOCABULARY_PATH.read_text(encoding="utf-8"))
SECRET_FIELD_PHRASES: Final[tuple[str, ...]] = tuple(_VOCABULARY["phrases"])
_PHRASE_TOKENS: Final[tuple[tuple[str, ...], ...]] = tuple(
    tuple(phrase.split()) for phrase in SECRET_FIELD_PHRASES
)
#: Identifiers that are secret only when they are the whole name.
SECRET_EXACT_NAMES: Final[tuple[str, ...]] = tuple(_VOCABULARY.get("exact_names", ()))
_EXACT_TOKENS: Final[frozenset[tuple[str, ...]]] = frozenset(
    tuple(name.split()) for name in SECRET_EXACT_NAMES
)
#: Identifiers that are secret when this is their last token.
SECRET_SUFFIX_NAMES: Final[frozenset[str]] = frozenset(
    _VOCABULARY.get("suffix_names", ())
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
    """True when the identifier contains a secret phrase as whole tokens,
    is exactly one of the exact names, or ends in a suffix name."""

    tokens = field_tokens(value)
    if tuple(tokens) in _EXACT_TOKENS:
        return True
    if tokens and tokens[-1] in SECRET_SUFFIX_NAMES:
        return True
    for phrase in _PHRASE_TOKENS:
        width = len(phrase)
        for start in range(len(tokens) - width + 1):
            if tuple(tokens[start : start + width]) == phrase:
                return True
    return False
