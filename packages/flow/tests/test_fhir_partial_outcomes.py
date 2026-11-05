"""Partial or unexplained search messages cannot certify a complete read."""

from __future__ import annotations

import pytest

from tests.test_fhir_read_policy import NEXT, read

OUTCOME_SYSTEM = "http://terminology.hl7.org/CodeSystem/operation-outcome"
ISSUE_SYSTEM = "http://hl7.org/fhir/issue-type"


def page(issue, *, matches=False, mode="outcome", next_url=None):
    outcome = {
        "resource": {
            "resourceType": "OperationOutcome",
            "id": "notice",
            "issue": [issue],
        }
    }
    if mode is not None:
        outcome["search"] = {"mode": mode}
    entries = [outcome]
    if matches:
        entries.insert(0, {"resource": {"resourceType": "Patient", "id": "one"}})
    result = {"resourceType": "Bundle", "type": "searchset", "entry": entries}
    if next_url:
        result["link"] = [{"relation": "next", "url": next_url}]
    return result


def no_match(severity="information"):
    coding = {"system": OUTCOME_SYSTEM, "code": "MSG_NO_MATCH"}
    return {
        "severity": severity,
        "code": "informational",
        "details": {"coding": [coding]},
    }


@pytest.mark.parametrize("kind", ["ensure", "effect"])
@pytest.mark.parametrize("severity", ["warning", "information"])
@pytest.mark.parametrize(
    "code",
    [
        "too-costly",
        "incomplete",
        "suppressed",
        "timeout",
        "throttled",
        "no-store",
        "exception",
    ],
)
def test_partial_issue_refuses_even_without_total(kind, severity, code):
    records, _ = read(kind, [page({"severity": severity, "code": code})])
    assert records is None


@pytest.mark.parametrize("kind", ["ensure", "effect"])
@pytest.mark.parametrize("severity", ["warning", "information"])
def test_unexplained_processing_warning_is_not_no_match(kind, severity):
    records, _ = read(kind, [page({"severity": severity, "code": "processing"})])
    assert records is None


@pytest.mark.parametrize("kind", ["ensure", "effect"])
@pytest.mark.parametrize("severity", ["warning", "information"])
def test_untyped_cap_message_does_not_certify_completeness(kind, severity):
    issue = {
        "severity": severity,
        "code": "processing",
        "diagnostics": "The search stopped at its resource limit.",
    }
    records, _ = read(kind, [page(issue)])
    assert records is None


@pytest.mark.parametrize("kind", ["ensure", "effect"])
@pytest.mark.parametrize("severity", ["warning", "information"])
def test_partial_coding_does_not_become_an_informational_note(kind, severity):
    issue = {
        "severity": severity,
        "code": "informational",
        "details": {"coding": [{"system": ISSUE_SYSTEM, "code": "incomplete"}]},
    }
    records, _ = read(kind, [page(issue)])
    assert records is None


@pytest.mark.parametrize("kind", ["ensure", "effect"])
@pytest.mark.parametrize("severity", ["warning", "information"])
@pytest.mark.parametrize("mode", ["outcome", None])
def test_normative_no_match_is_only_an_empty_search_note(kind, severity, mode):
    records, _ = read(kind, [page(no_match(severity), mode=mode)])
    assert records == []


@pytest.mark.parametrize("kind", ["ensure", "effect"])
@pytest.mark.parametrize("severity", ["warning", "information"])
@pytest.mark.parametrize("mode", ["outcome", None])
def test_informational_note_is_not_a_matching_resource(kind, severity, mode):
    issue = {"severity": severity, "code": "informational"}
    records, _ = read(kind, [page(issue, matches=True, mode=mode)])
    assert records is not None
    assert [r["id"] for r in records] == ["one"]


@pytest.mark.parametrize("kind", ["ensure", "effect"])
@pytest.mark.parametrize("severity", ["warning", "information"])
def test_no_match_cannot_coexist_with_matching_records(kind, severity):
    records, _ = read(kind, [page(no_match(severity), matches=True)])
    assert records is None


@pytest.mark.parametrize("kind", ["ensure", "effect"])
def test_earlier_no_match_is_rechecked_when_a_later_page_has_matches(kind):
    later = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": {"resourceType": "Patient", "id": "one"}}],
    }
    records, _ = read(kind, [page(no_match(), next_url=NEXT), later])
    assert records is None


@pytest.mark.parametrize("kind", ["ensure", "effect"])
def test_partial_outcome_without_mode_is_still_unreadable(kind):
    records, _ = read(
        kind, [page({"severity": "warning", "code": "incomplete"}, mode=None)]
    )
    assert records is None


@pytest.mark.parametrize("kind", ["ensure", "effect"])
def test_unknown_issue_code_does_not_prove_completeness(kind):
    records, _ = read(
        kind, [page({"severity": "information", "code": "unrecognized-search-note"})]
    )
    assert records is None


@pytest.mark.parametrize("kind", ["ensure", "effect"])
def test_outcome_mode_cannot_hide_another_resource_type(kind):
    body = page({"severity": "information", "code": "informational"})
    body["entry"][0]["resource"]["resourceType"] = "Patient"
    records, _ = read(kind, [body])
    assert records is None


@pytest.mark.parametrize("kind", ["ensure", "effect"])
@pytest.mark.parametrize("severity", ["warning", "information"])
@pytest.mark.parametrize("code", ["processing", "not-found"])
def test_no_match_detail_cannot_override_a_noninformational_issue(kind, severity, code):
    issue = no_match(severity)
    issue["code"] = code
    records, _ = read(kind, [page(issue)])
    assert records is None
