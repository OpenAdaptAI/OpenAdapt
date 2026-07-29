"""Configuration for privacy scrubbing.

This module provides configuration settings for PII/PHI scrubbing operations.
Settings can be customized by creating a new PrivacyConfig instance or modifying
the global `config` instance.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from threading import RLock
from typing import Iterator, Sequence


class ScrubbingPolicyChanged(RuntimeError):
    """A scrubber attempted to mutate policy during a scrub operation."""


class _FrozenList(list):
    """List-compatible policy value that refuses in-operation mutation."""

    def _refuse(self, *_args, **_kwargs) -> None:
        raise ScrubbingPolicyChanged("privacy policy is immutable during a scrub operation")

    __setitem__ = __delitem__ = append = clear = extend = insert = pop = remove = reverse = sort = (
        _refuse
    )
    __iadd__ = __imul__ = _refuse

    def __deepcopy__(self, _memo):
        return self


class _FrozenDict(dict):
    """Dict-compatible policy value that refuses in-operation mutation."""

    def _refuse(self, *_args, **_kwargs) -> None:
        raise ScrubbingPolicyChanged("privacy policy is immutable during a scrub operation")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = _refuse

    def __deepcopy__(self, _memo):
        return self


def _freeze_policy_value(value):
    if isinstance(value, dict):
        return _FrozenDict({key: _freeze_policy_value(item) for key, item in value.items()})
    if isinstance(value, list):
        return _FrozenList(_freeze_policy_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_policy_value(item) for item in value)
    return value


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
        SCRUB_CONFIG_TRF: Presidio NLP engine configuration. The historical
            attribute name is retained for compatibility; the configured model
            is intentionally a non-transformer pipeline.
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

    # Presidio NLP engine configuration. Keep this in lockstep with
    # SPACY_MODEL_NAME; the provider validates both before loading anything.
    SCRUB_CONFIG_TRF: dict = field(
        default_factory=lambda: {
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
        }
    )

    # Entity types to ignore during Presidio scrubbing
    SCRUB_PRESIDIO_IGNORE_ENTITIES: Sequence[str] = field(default_factory=list)

    # SpaCy model name
    SPACY_MODEL_NAME: str = "en_core_web_sm"

    def __setattr__(self, name: str, value) -> None:
        if self.__dict__.get("_policy_locked", False) and not name.startswith("_"):
            raise ScrubbingPolicyChanged("privacy policy is immutable during a scrub operation")
        object.__setattr__(self, name, value)

    def policy_digest(self) -> str:
        """Return a stable digest of the complete effective scrub policy."""
        payload = json.dumps(
            asdict(self),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


# Global default configuration instance
config = PrivacyConfig()

_operation_lock = RLock()

_operation_config: ContextVar[PrivacyConfig | None] = ContextVar(
    "openadapt_privacy_operation_config",
    default=None,
)


def effective_config() -> PrivacyConfig:
    """Return the immutable-per-operation policy, or the process default."""
    return _operation_config.get() or config


@contextmanager
def privacy_operation() -> Iterator[PrivacyConfig]:
    """Bind one deep-copied policy snapshot for a complete scrub operation."""
    existing = _operation_config.get()
    if existing is not None:
        yield existing
        return

    # Serialize policy admission. During the operation both the explicit
    # snapshot and the legacy public ``config`` object are read-compatible but
    # mutation-proof, so older third-party providers cannot silently mix two
    # policies and still receive completed evidence.
    with _operation_lock:
        snapshot = deepcopy(config)
        global_values = {name: getattr(config, name) for name in asdict(config)}
        for name, value in asdict(snapshot).items():
            object.__setattr__(snapshot, name, _freeze_policy_value(value))
        object.__setattr__(snapshot, "_policy_locked", True)

        for name, value in global_values.items():
            object.__setattr__(config, name, _freeze_policy_value(deepcopy(value)))
        object.__setattr__(config, "_policy_locked", True)

        token = _operation_config.set(snapshot)
        try:
            yield snapshot
        finally:
            _operation_config.reset(token)
            object.__setattr__(config, "_policy_locked", False)
            for name, value in global_values.items():
                object.__setattr__(config, name, value)
