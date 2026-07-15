"""Quantitative synthetic PHI recall and clean-text regression gate."""

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


PHI_CASES = [
    ("person", "Patient John Smith is ready.", "John Smith"),
    ("person", "Schedule Amelia Earhart for follow-up.", "Amelia Earhart"),
    ("person", "The chart belongs to Aisha Rahman.", "Aisha Rahman"),
    ("person", "Emergency contact is José Alvarez.", "José Alvarez"),
    ("person", "Seen by physician Chidi Okafor today.", "Chidi Okafor"),
    ("person", "Send the referral to Mei Lin.", "Mei Lin"),
    ("person", "Patient Olivia O'Connor arrived.", "Olivia O'Connor"),
    ("person", "Discuss results with François Dupont.", "François Dupont"),
    ("email", "Email jane.doe@example.com with results.", "jane.doe@example.com"),
    (
        "email",
        "Portal contact: care-team+east@clinic.example.org",
        "care-team+east@clinic.example.org",
    ),
    ("phone", "Call the patient at 555-123-4567.", "555-123-4567"),
    ("phone", "Mobile: +1 (416) 555-0199", "+1 (416) 555-0199"),
    ("ssn", "Social security number 923-45-6789.", "923-45-6789"),
    (
        "credit_card",
        "Card 4532-1234-5678-9012 is on file.",
        "4532-1234-5678-9012",
    ),
    ("dob", "Date of birth: 01/15/1985.", "01/15/1985"),
    ("dob", "DOB is January 5, 1974.", "January 5, 1974"),
    (
        "address",
        "Home address: 123 Main Street, Boston, MA 02110.",
        "123 Main Street",
    ),
    ("address", "Mail to 88 King St W, Toronto, ON M5H 1J9.", "88 King St W"),
    ("ip", "Last login from 192.168.10.22.", "192.168.10.22"),
    (
        "url",
        "Portal is https://patient.example.org/chart/42",
        "https://patient.example.org/chart/42",
    ),
    ("mrn", "Medical record number MRN: 00123456.", "00123456"),
    ("mrn", "Patient ID: AB-902771.", "AB-902771"),
    ("member_id", "Health plan member ID ZXQ-443-991.", "ZXQ-443-991"),
    ("license", "Provider license number ME123456.", "ME123456"),
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
    for category, text, identifier in PHI_CASES:
        scrubbed = scrubber.scrub_text(text)
        if identifier in scrubbed:
            misses.append((category, identifier, scrubbed))

    hits = len(PHI_CASES) - len(misses)
    recall = hits / len(PHI_CASES)
    assert recall == 1.0, (
        f"synthetic PHI recall {hits}/{len(PHI_CASES)} ({recall:.1%}); misses={misses}"
    )


@pytest.mark.parametrize("text", CLEAN_CASES)
def test_clean_operational_text_is_not_redacted(scrubber, text: str) -> None:
    assert scrubber.scrub_text(text) == text
