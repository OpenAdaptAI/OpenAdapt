"""Quantitative synthetic PHI recall and clean-text regression gate.

Recall alone is not enough. A caller that routes on the entity type needs the
type to be right, so every case below pins the placeholder it must produce.
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

    from openadapt_privacy.providers.presidio import PresidioScrubbingProvider
except ImportError:
    pytest.skip("Presidio dependencies not installed", allow_module_level=True)


# (category, text, identifier that must disappear, entity type that must fire)
PHI_CASES = [
    ("person", "Patient John Smith is ready.", "John Smith", "PERSON"),
    ("person", "Schedule Amelia Earhart for follow-up.", "Amelia Earhart", "PERSON"),
    ("person", "The chart belongs to Aisha Rahman.", "Aisha Rahman", "PERSON"),
    ("person", "Emergency contact is José Alvarez.", "José Alvarez", "PERSON"),
    ("person", "Seen by physician Chidi Okafor today.", "Chidi Okafor", "PERSON"),
    ("person", "Send the referral to Mei Lin.", "Mei Lin", "PERSON"),
    ("person", "Patient Olivia O'Connor arrived.", "Olivia O'Connor", "PERSON"),
    ("person", "Discuss results with François Dupont.", "François Dupont", "PERSON"),
    ("email", "Email jane.doe@example.com with results.", "jane.doe@example.com", "EMAIL_ADDRESS"),
    (
        "email",
        "Portal contact: care-team+east@clinic.example.org",
        "care-team+east@clinic.example.org",
        "EMAIL_ADDRESS",
    ),
    ("phone", "Call the patient at 555-123-4567.", "555-123-4567", "PHONE_NUMBER"),
    ("phone", "Mobile: +1 (416) 555-0199", "+1 (416) 555-0199", "PHONE_NUMBER"),
    ("ssn", "Social security number 923-45-6789.", "923-45-6789", "US_SSN"),
    (
        "credit_card",
        "Card 4532-1234-5678-9014 is on file.",
        "4532-1234-5678-9014",
        "CREDIT_CARD",
    ),
    (
        "credit_card",
        "Card 4111111111111111 on file",
        "4111111111111111",
        "CREDIT_CARD",
    ),
    (
        "bank_account",
        "My bank account number is 635526789012.",
        "635526789012",
        "US_BANK_NUMBER",
    ),
    ("dob", "Date of birth: 01/15/1985.", "01/15/1985", "DATE_TIME"),
    ("dob", "DOB is January 5, 1974.", "January 5, 1974", "DATE_TIME"),
    (
        "address",
        "Home address: 123 Main Street, Boston, MA 02110.",
        "123 Main Street",
        "STREET_ADDRESS",
    ),
    ("address", "Mail to 88 King St W, Toronto, ON M5H 1J9.", "88 King St W", "STREET_ADDRESS"),
    ("ip", "Last login from 192.168.10.22.", "192.168.10.22", "IP_ADDRESS"),
    (
        "url",
        "Portal is https://patient.example.org/chart/42",
        "https://patient.example.org/chart/42",
        "URL",
    ),
    ("mrn", "Medical record number MRN: 00123456.", "00123456", "MEDICAL_RECORD_NUMBER"),
    ("mrn", "Patient ID: AB-902771.", "AB-902771", "MEDICAL_RECORD_NUMBER"),
    ("member_id", "Health plan member ID ZXQ-443-991.", "ZXQ-443-991", "MEDICAL_RECORD_NUMBER"),
    ("license", "Provider license number ME123456.", "ME123456", "US_DRIVER_LICENSE"),
]

CLEAN_CASES = [
    "The quick brown fox jumps over the lazy dog.",
    "Click Save to continue.",
    "Click Submit to continue.",
    "Select Patient from the menu.",
    "Open Settings and choose Privacy.",
    "Press Cancel to return.",
    "The workflow completed 12 steps successfully.",
    "Retry after the modal closes.",
    "Invoice total is 42 dollars.",
]


@pytest.fixture(scope="module")
def scrubber() -> PresidioScrubbingProvider:
    return PresidioScrubbingProvider()


def test_synthetic_phi_identifier_recall_is_complete(scrubber) -> None:
    misses = []
    for category, text, identifier, _entity in PHI_CASES:
        scrubbed = scrubber.scrub_text(text)
        if identifier in scrubbed:
            misses.append((category, identifier, scrubbed))

    hits = len(PHI_CASES) - len(misses)
    recall = hits / len(PHI_CASES)
    assert recall == 1.0, (
        f"synthetic PHI recall {hits}/{len(PHI_CASES)} ({recall:.1%}); misses={misses}"
    )


def test_synthetic_phi_entity_labels_are_correct(scrubber) -> None:
    """The placeholder must name the entity a policy would route on.

    Asserting only that the identifier disappeared hid a systematic
    mislabelling: a card number came back as ``<DATE_TIME>``.
    """
    wrong = []
    for category, text, _identifier, entity in PHI_CASES:
        scrubbed = scrubber.scrub_text(text)
        if f"<{entity}>" not in scrubbed:
            wrong.append((category, entity, scrubbed))

    assert not wrong, f"{len(wrong)}/{len(PHI_CASES)} cases carry the wrong entity type: {wrong}"


@pytest.mark.parametrize("text", CLEAN_CASES)
def test_clean_operational_text_is_not_redacted(scrubber, text: str) -> None:
    assert scrubber.scrub_text(text) == text


def test_a_card_number_that_fails_the_luhn_check_is_still_redacted(scrubber) -> None:
    """Documented limit: no checksum, no ``CREDIT_CARD`` label.

    ``4532-1234-5678-9012`` is not a valid card number, so Presidio's card
    recognizer invalidates it. Something else claims the span, and the digits
    still leave the output, but the type is not ``CREDIT_CARD``.
    """
    scrubbed = scrubber.scrub_text("Card: 4532-1234-5678-9012")

    assert "4532-1234-5678-9012" not in scrubbed
    assert "<CREDIT_CARD>" not in scrubbed


def test_a_wider_ner_span_survives_when_it_holds_digits_the_pattern_missed() -> None:
    """Relabelling must never shrink what gets redacted.

    Presidio's driver-license recognizer matches only the leading ``A123`` of
    ``A123-456-789-012``. If an overlapping ``ORGANIZATION`` span covering the
    whole identifier were dropped in favour of that partial match, the trailing
    digits would leave the scrubber unredacted.
    """
    from presidio_analyzer import RecognizerResult

    from openadapt_privacy.providers.presidio import _prefer_deterministic_identifier_labels

    text = "A123-456-789-012"
    partial = RecognizerResult(entity_type="US_DRIVER_LICENSE", start=0, end=4, score=0.65)
    whole = RecognizerResult(entity_type="ORGANIZATION", start=0, end=16, score=0.85)

    kept = _prefer_deterministic_identifier_labels(text, [partial, whole])

    assert whole in kept


def test_a_validated_pattern_takes_the_label_from_an_overlapping_ner_span() -> None:
    from presidio_analyzer import RecognizerResult

    from openadapt_privacy.providers.presidio import _prefer_deterministic_identifier_labels

    text = "Card 4111111111111111 on file"
    card = RecognizerResult(entity_type="CREDIT_CARD", start=5, end=21, score=1.0)
    date = RecognizerResult(entity_type="DATE_TIME", start=0, end=21, score=0.85)

    kept = _prefer_deterministic_identifier_labels(text, [card, date])

    assert kept == [card]


def test_a_low_confidence_pattern_does_not_take_a_label() -> None:
    """An unvalidated 0.05 digit-run match is a guess, not an identifier."""
    from presidio_analyzer import RecognizerResult

    from openadapt_privacy.providers.presidio import _prefer_deterministic_identifier_labels

    text = "Card 4111111111111111 on file"
    weak = RecognizerResult(entity_type="US_BANK_NUMBER", start=5, end=21, score=0.05)
    date = RecognizerResult(entity_type="DATE_TIME", start=0, end=21, score=0.85)

    kept = _prefer_deterministic_identifier_labels(text, [weak, date])

    assert date in kept
