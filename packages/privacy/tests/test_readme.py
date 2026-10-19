"""Keep the README honest: its limits stay stated and its examples stay real."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
COMPLIANCE = (ROOT / "docs" / "COMPLIANCE.md").read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    match = re.search(rf"(?ms)^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text)
    assert match is not None, f"missing section: {heading}"
    return " ".join(match.group(1).split())


def test_readme_states_its_limits_in_one_section() -> None:
    limits = _section(README, "Limits")
    assert "synthetic, not clinical" in limits
    assert "does not claim clinical validation" in limits


def test_readme_documents_the_scan_exit_codes() -> None:
    scan = _section(README, "Block identifiers in CI")
    assert "openadapt-privacy-scan --self-test" in scan
    assert "--allow-empty" in scan


def test_readme_links_the_compliance_notes_once() -> None:
    assert len(re.findall(r"\]\([^)]*docs/COMPLIANCE\.md\)", README)) == 1
    compliance = " ".join(COMPLIANCE.split())
    assert "does not claim clinical validation" in compliance
    assert "synthetic, not clinical" in compliance


def test_readme_leaves_operator_language_to_the_compliance_notes() -> None:
    lowered = README.lower()
    for term in ("admission", "admitted", "ledger", "evidence authority", "product target"):
        assert term not in lowered, term
    assert "lifecycle" not in lowered


def test_readme_recall_count_matches_the_test_set() -> None:
    # Read the count from source: importing the module skips it without spaCy.
    tree = ast.parse((ROOT / "tests" / "test_phi_recall.py").read_text(encoding="utf-8"))
    cases = next(
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "PHI_CASES" for target in node.targets
        )
    )
    assert isinstance(cases, ast.List)

    assert f"{len(cases.elts)} synthetic identifiers" in README
    assert f"{len(cases.elts)} synthetic identifiers" in COMPLIANCE


try:
    import spacy

    from openadapt_privacy.config import config

    _HAS_MODEL = spacy.util.is_package(config.SPACY_MODEL_NAME)
except ImportError:
    _HAS_MODEL = False

needs_model = pytest.mark.skipif(not _HAS_MODEL, reason="Presidio and the spaCy model are needed")


@pytest.fixture(scope="module")
def scrubber():
    from openadapt_privacy import PresidioScrubbingProvider

    return PresidioScrubbingProvider()


@needs_model
def test_quick_start_output_is_what_the_scrubber_returns(scrubber) -> None:
    text = "Contact John Smith at john.smith@example.com or 555-123-4567"
    output = "Contact <PERSON> at <EMAIL_ADDRESS> or <PHONE_NUMBER>"
    assert f'print(scrubber.scrub_text("{text}"))' in README
    assert f"# {output}" in README
    assert scrubber.scrub_text(text) == output


@needs_model
@pytest.mark.parametrize(
    ("call", "value", "output"),
    [
        (
            'scrub_dict({"text": "Email: john@example.com"}, scrubber)',
            {"text": "Email: john@example.com"},
            {"text": "Email: <EMAIL_ADDRESS>"},
        ),
        (
            'scrub_dict({"text": "-".join("john@example.com")}, scrubber)',
            {"text": "-".join("john@example.com")},
            {"text": "<-E-M-A-I-L-_-A-D-D-R-E-S-S->"},
        ),
    ],
)
def test_keystroke_examples_are_what_scrub_dict_returns(scrubber, call, value, output) -> None:
    from openadapt_privacy import scrub_dict

    assert f"{call}\n# {output!r}" in README
    assert scrub_dict(value, scrubber) == output


@needs_model
def test_element_tree_example_is_what_scrub_dict_returns(scrubber) -> None:
    from openadapt_privacy import scrub_dict

    scrubbed = scrub_dict(
        {
            "title": "User Profile - John Smith",
            "tooltip": "Click to contact john@example.com",
            "value": "Call 555-123-4567",
            "coordinates": {"x": 100, "y": 200},
        },
        scrubber,
    )
    assert scrubbed == {
        "title": "User Profile - <PERSON>",
        "tooltip": "Click to contact <EMAIL_ADDRESS>",
        "value": "Call <PHONE_NUMBER>",
        "coordinates": {"x": 100, "y": 200},
    }
    for key, value in scrubbed.items():
        assert f"{key!r}: {value!r}" in README


@needs_model
def test_recording_example_is_what_recording_scrub_returns(scrubber) -> None:
    from openadapt_privacy import DictRecordingLoader

    recording = DictRecordingLoader().load_from_dict(
        {
            "task_description": "Send email to John Smith at john@example.com",
            "actions": [
                {"id": 1, "action_type": "click", "text": "Compose", "timestamp": 1000},
                {
                    "id": 2,
                    "action_type": "type",
                    "text": "-".join("john@example.com"),
                    "timestamp": 2000,
                },
            ],
        }
    )
    scrubbed = recording.scrub(scrubber)
    assert scrubbed.task_description == "Send email to <PERSON> at <EMAIL_ADDRESS>"
    assert f"# {scrubbed.task_description!r}" in README


@needs_model
def test_luhn_limit_examples_are_what_the_scrubber_returns(scrubber) -> None:
    limits = _section(README, "Limits")
    assert "`4532-1234-5678-9012` becomes `<DATE_TIME>`" in limits
    assert "`4111 1111 1111 1112` keeps its first eight digits" in limits

    assert scrubber.scrub_text("4532-1234-5678-9012") == "<DATE_TIME>"
    partial = scrubber.scrub_text("4111 1111 1111 1112")
    assert partial.startswith("4111 1111 ")
    assert "1111 1112" not in partial
    assert "<CREDIT_CARD>" not in partial
