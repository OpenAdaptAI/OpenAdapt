"""Separated action text must be decided by the value's shape, not the key name.

``SCRUB_KEYS_HTML`` contains ``text``, which is also the most common key name in
an arbitrary dict. Treating every ``text`` value as a hyphen-joined key sequence
turned a plain sentence into one character per hyphen.
"""

from __future__ import annotations

import pytest

try:
    import spacy

    from openadapt_privacy.config import config

    if not spacy.util.is_package(config.SPACY_MODEL_NAME):
        pytest.skip(
            f"SpaCy model {config.SPACY_MODEL_NAME} not installed",
            allow_module_level=True,
        )

    from openadapt_privacy.pipelines.dicts import scrub_dict
    from openadapt_privacy.providers.presidio import PresidioScrubbingProvider
except ImportError:  # pragma: no cover - exercised only without the extra
    pytest.skip("Presidio dependencies not installed", allow_module_level=True)


@pytest.fixture(scope="module")
def scrubber() -> PresidioScrubbingProvider:
    return PresidioScrubbingProvider()


PROSE_UNDER_TEXT_KEY = "Email: john@example.com"


def test_plain_prose_under_the_text_key_is_not_character_separated(scrubber) -> None:
    """A sentence under ``text`` comes back as a sentence."""
    result = scrub_dict({"text": PROSE_UNDER_TEXT_KEY}, scrubber)["text"]

    assert "john@example.com" not in result
    assert "<EMAIL_ADDRESS>" in result
    assert "-E-M-A-I-L" not in result
    assert result.count("-") == PROSE_UNDER_TEXT_KEY.count("-")


def test_hyphenated_prose_under_the_text_key_keeps_its_words(scrubber) -> None:
    """A single hyphen in ordinary prose does not make the value action text."""
    result = scrub_dict({"text": "Book the follow-up for jane.doe@example.com"}, scrubber)["text"]

    assert "follow-up" in result
    assert "<EMAIL_ADDRESS>" in result


def test_separated_action_text_still_round_trips(scrubber) -> None:
    """A genuine key sequence is still joined before analysis and re-separated."""
    typed = "-".join("john@example.com")

    result = scrubber.scrub_text(typed, is_separated=True)

    assert "".join(result.split("-")) == "<EMAIL_ADDRESS>"


def test_separated_action_text_under_the_text_key_still_finds_pii(scrubber) -> None:
    """The dict path must not lose the separated analysis that hides PII."""
    typed = "-".join("john@example.com")

    result = scrub_dict({"text": typed}, scrubber)["text"]

    assert "john@example.com" not in "".join(result.split("-"))
    assert "".join(result.split("-")) == "<EMAIL_ADDRESS>"


def test_separated_handling_can_be_turned_off_per_call(scrubber) -> None:
    """``separated_keys`` makes the behaviour explicit instead of implied."""
    typed = "-".join("john@example.com")

    result = scrub_dict({"text": typed}, scrubber, separated_keys=[])["text"]

    assert result == typed


def test_a_phone_number_under_the_text_key_survives_as_a_phone_number(scrubber) -> None:
    """Hyphen groups of more than one character are not a key sequence."""
    result = scrub_dict({"text": "Call 555-123-4567 now"}, scrubber)["text"]

    assert "555-123-4567" not in result
    assert "<PHONE_NUMBER>" in result
