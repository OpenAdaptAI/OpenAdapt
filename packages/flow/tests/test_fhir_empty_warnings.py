"""Zero reported matches do not qualify an unexplained processing warning.

These synthetic responses preserve the warning/processing/zero-total shape.
They are not evidence that a native service's search completed successfully.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from openadapt_flow.ensure_record import FhirReader, Unreadable, find_matches
from tests.test_ensure_record import PARAMS, contract
from tests.test_fhir_partial_outcomes import no_match, page
from tests.test_fhir_read_policy import NEXT, read


def empty_warning(**issue_fields):
    issue = {"severity": "warning", "code": "processing", **issue_fields}
    return {**page(issue), "total": 0}


@pytest.mark.parametrize("kind", ["ensure", "effect"])
@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"details": {"text": "No records matched the search."}},
        {"diagnostics": "No records matched the search."},
        {"diagnostics": "The search stopped at its resource limit."},
    ],
)
def test_zero_total_does_not_explain_a_processing_warning(kind, extra):
    records, _ = read(kind, [empty_warning(**extra)])
    assert records is None


@pytest.mark.parametrize("kind", ["ensure", "effect"])
@pytest.mark.parametrize("severity", ["warning", "information", "error", "fatal"])
@pytest.mark.parametrize(
    "code",
    [
        "incomplete",
        "timeout",
        "too-costly",
        "suppressed",
        "security",
        "forbidden",
        "not-supported",
        "exception",
        "no-store",
        "throttled",
    ],
)
def test_zero_total_cannot_hide_failed_or_partial_processing(kind, severity, code):
    records, _ = read(kind, [empty_warning(severity=severity, code=code)])
    assert records is None


@pytest.mark.parametrize("kind", ["ensure", "effect"])
@pytest.mark.parametrize("total", [None, True, -1, "0"])
def test_an_ambiguous_total_cannot_qualify_the_warning(kind, total):
    body = empty_warning()
    body["total"] = total
    records, _ = read(kind, [body])
    assert records is None


@pytest.mark.parametrize("kind", ["ensure", "effect"])
def test_a_missing_total_cannot_qualify_the_warning(kind):
    body = empty_warning()
    del body["total"]
    records, _ = read(kind, [body])
    assert records is None


@pytest.mark.parametrize("kind", ["ensure", "effect"])
@pytest.mark.parametrize(
    "issues",
    [
        None,
        [],
        {},
        [None],
        ["warning"],
        [{"code": "processing"}],
        [{"severity": "warning"}],
        [{"severity": "warning", "code": "unknown-code"}],
        [
            {"severity": "information", "code": "informational"},
            {"severity": "fatal", "code": "processing"},
        ],
    ],
)
def test_zero_total_does_not_repair_a_malformed_or_failed_outcome(kind, issues):
    body = empty_warning()
    body["entry"][0]["resource"]["issue"] = issues
    records, _ = read(kind, [body])
    assert records is None


@pytest.mark.parametrize("kind", ["ensure", "effect"])
@pytest.mark.parametrize("severity", ["warning", "information"])
def test_a_qualified_empty_note_still_allows_an_empty_result(kind, severity):
    body = {**page(no_match(severity)), "total": 0}
    records, _ = read(kind, [body])
    assert records == []


@pytest.mark.parametrize("kind", ["ensure", "effect"])
def test_zero_total_and_a_no_match_note_cannot_hide_a_returned_match(kind):
    body = {**page(no_match(), matches=True), "total": 0}
    records, _ = read(kind, [body])
    assert records is None


@pytest.mark.parametrize("kind", ["ensure", "effect"])
@pytest.mark.parametrize(
    "links",
    [
        [{"relation": "next", "url": "https://another.test/fhir/Patient"}],
        [{"relation": "next", "url": NEXT}] * 2,
        [{"relation": "next"}],
    ],
)
def test_even_a_qualified_zero_result_cannot_hide_unsupported_pagination(kind, links):
    body = {**page(no_match()), "total": 0, "link": links}
    records, _ = read(kind, [body])
    assert records is None


def test_an_empty_note_cannot_erase_a_freshly_known_matching_record():
    body = {**page(no_match()), "total": 0}
    reader = FhirReader(contract(), lambda url, params: (200, {}, deepcopy(body)))
    with pytest.raises(Unreadable, match="known matching record"):
        find_matches(contract(), reader, PARAMS, expected_ids=["known"])


@pytest.mark.parametrize("kind", ["ensure", "effect"])
def test_a_coded_processing_warning_still_needs_completeness_evidence(kind):
    # Preserve the observed empty-response structure; all values are synthetic.
    body = empty_warning(
        details={
            "coding": [
                {
                    "system": "https://fhir.test/CodeSystem/search-notice",
                    "code": "NO_RESULTS",
                }
            ],
            "text": "No records matched the search.",
        }
    )
    body["link"] = [
        {"relation": "self", "url": "http://fhir.test/fhir/R4/Patient?family=NoMatch"}
    ]
    records, _ = read(kind, [body])
    assert records is None
