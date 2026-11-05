"""Shared read directives and whole-search refusals; no native qualification."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from openadapt_flow.ensure_record import FhirReader, Unreadable
from openadapt_flow.runtime.effects.fhir import FhirEffectVerifier
from tests.test_ensure_record import contract

BASE = "http://fhir.test/fhir/R4"
NEXT = BASE + "/Patient?page=2"


class Session:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        body = self.pages.pop(0)
        return SimpleNamespace(
            status_code=200, headers={}, url=url, json=lambda: deepcopy(body)
        )


def bundle(ids, *, total=101, links=None):
    body = {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": total,
        "entry": [{"resource": {"resourceType": "Patient", "id": str(i)}} for i in ids],
    }
    if links is not None:
        body["link"] = links
    return body


def read(kind, pages):
    session = Session(pages)
    if kind == "ensure":
        reader = FhirReader(contract())
        reader._client = session
        try:
            records = reader.resources("Patient")
        except Unreadable:
            records = None
    else:
        reader = FhirEffectVerifier(
            BASE,
            resource_type="Patient",
            field_paths={"id": "id"},
            session=session,
            access_token="synthetic-test-token",
        )
        records = reader._search()
    return records, session.calls


@pytest.mark.parametrize("kind", ["ensure", "effect"])
def test_every_page_requests_no_cache_and_reads_over_one_hundred(kind):
    records, calls = read(
        kind,
        [
            bundle(range(60), links=[{"relation": "next", "url": NEXT}]),
            bundle(range(60, 101)),
        ],
    )
    assert records is not None
    assert [r["id"] for r in records] == [str(i) for i in range(101)]
    assert [url for url, _ in calls] == [BASE + "/Patient", NEXT]
    for _, kwargs in calls:
        assert kwargs["headers"]["Cache-Control"] == "no-cache"
        assert "no-store" not in kwargs["headers"]["Cache-Control"]
        assert kwargs["headers"]["Accept"] == "application/fhir+json"
        if kind == "effect":
            assert kwargs["headers"]["Authorization"] == "Bearer synthetic-test-token"
            assert kwargs["verify"] is True


@pytest.mark.parametrize("kind", ["ensure", "effect"])
def test_one_hundred_result_cap_is_incomplete(kind):
    records, _ = read(kind, [bundle(range(100))])
    assert records is None


@pytest.mark.parametrize("kind", ["ensure", "effect"])
@pytest.mark.parametrize(
    "links",
    [
        [{"relation": "next", "url": 7}],
        [{"relation": "next", "url": ""}],
        [{"relation": "next"}],
        [{"relation": "next", "url": NEXT}] * 2,
        {"relation": "next", "url": NEXT},
    ],
)
def test_malformed_next_link_cannot_disguise_truncation(kind, links):
    records, _ = read(kind, [bundle([1], total=1, links=links), bundle([], total=1)])
    assert records is None


@pytest.mark.parametrize("kind", ["ensure", "effect"])
def test_null_link_is_not_a_qualified_end(kind):
    body = bundle([1], total=1)
    body["link"] = None
    records, _ = read(kind, [body])
    assert records is None


@pytest.mark.parametrize("kind", ["ensure", "effect"])
def test_changing_total_cannot_certify_the_search(kind):
    records, _ = read(
        kind,
        [
            bundle(range(60), links=[{"relation": "next", "url": NEXT}]),
            bundle(range(60, 101), total=100),
        ],
    )
    assert records is None


@pytest.mark.parametrize("kind", ["ensure", "effect"])
@pytest.mark.parametrize("total", [True, -1, "101"])
def test_invalid_total_is_not_a_coverage_proof(kind, total):
    records, _ = read(kind, [bundle([1], total=total)])
    assert records is None


@pytest.mark.parametrize("name", ["Cache-Control", "cache-control", "CACHE-CONTROL"])
@pytest.mark.parametrize("value", ["no-store", "no-cache, no-store", "max-age=60"])
def test_private_read_headers_cannot_replace_freshness(name, value):
    from openadapt_flow.fhir_read import fhir_read_headers

    with pytest.raises(ValueError, match="no-cache only"):
        fhir_read_headers({name: value, "Prefer": "return=representation"})


def test_casefold_duplicate_cache_directives_are_not_merged():
    from openadapt_flow.fhir_read import fhir_read_headers

    with pytest.raises(ValueError, match="repeated case-insensitively"):
        fhir_read_headers({"Cache-Control": "no-cache", "cache-control": "no-cache"})


def test_compatible_private_header_is_one_canonical_directive():
    from openadapt_flow.fhir_read import fhir_read_headers

    headers = fhir_read_headers(
        {
            "cache-control": " NO-CACHE ",
            "Prefer": "return=representation",
            "Authorization": "Bearer synthetic-test-token",
        }
    )
    assert headers == {
        "Accept": "application/fhir+json",
        "Cache-Control": "no-cache",
        "Prefer": "return=representation",
        "Authorization": "Bearer synthetic-test-token",
    }
    assert [name for name in headers if name.casefold() == "cache-control"] == [
        "Cache-Control"
    ]
