"""Synthetic FHIR R4 discovery controls; no runtime service or read qualification."""

from __future__ import annotations

import logging
from copy import deepcopy
from itertools import permutations
from typing import Any

import pytest
from openadapt_flow.fhir_setup import learn_fhir_read, search_resources

BASE = "https://example.test/fhir"


def resource(
    name: str, interactions: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "type": name,
        "interaction": interactions
        if interactions is not None
        else [{"code": "read"}, {"code": "search-type"}],
    }


def capability(*resources: dict[str, Any]) -> dict[str, Any]:
    return {
        "resourceType": "CapabilityStatement",
        "fhirVersion": "4.0.1",
        "kind": "instance",
        "implementation": {"url": BASE},
        "rest": [{"mode": "server", "resource": list(resources)}],
    }


def test_repeated_operation_interaction_preserves_patient_candidate(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Normative resource/interaction vocabulary only. This minimizes the
    # observed declaration shape without copying service metadata or URLs.
    statement = capability(
        resource(
            "OperationDefinition",
            [
                {"code": "search-type"},
                {"code": "read"},
                {"code": "search-type"},
                {"code": "delete"},
            ],
        ),
        resource(
            "Patient",
            [
                {"code": "create"},
                {"code": "update"},
                {"code": "search-type"},
                {"code": "read"},
            ],
        ),
    )
    original = deepcopy(statement)
    with caplog.at_level(logging.WARNING):
        assert search_resources(statement, BASE) == ["OperationDefinition", "Patient"]
    assert len(caplog.records) == 1
    assert "identical_interactions_collapsed" in caplog.records[0].getMessage()
    assert statement == original


@pytest.mark.parametrize(
    "first,second",
    [
        ({"documentation": "one"}, {"documentation": "two"}),
        (
            {"extension": [{"url": "urn:mode", "valueBoolean": True}]},
            {"extension": [{"url": "urn:mode", "valueBoolean": False}]},
        ),
        (
            {"extension": [{"url": "urn:value", "value": True}]},
            {"extension": [{"url": "urn:value", "value": 1}]},
        ),
        (
            {"extension": [{"url": "urn:value", "value": 1}]},
            {"extension": [{"url": "urn:value", "value": 1.0}]},
        ),
        ({"documentation": None}, {}),
        (
            {"extension": [{"url": "urn:first"}, {"url": "urn:second"}]},
            {"extension": [{"url": "urn:second"}, {"url": "urn:first"}]},
        ),
        (
            {"_code": {"extension": [{"url": "urn:source", "valueString": "a"}]}},
            {"_code": {"extension": [{"url": "urn:source", "valueString": "b"}]}},
        ),
    ],
)
def test_full_interaction_conflict_refuses_only_its_resource(
    first: dict[str, Any], second: dict[str, Any]
) -> None:
    diagnostics: list[dict[str, str]] = []
    statement = capability(
        resource(
            "Patient",
            [
                {"code": "read"},
                {"code": "search-type", **first},
                {"code": "search-type", **second},
            ],
        ),
        resource("Observation"),
    )
    assert search_resources(statement, BASE, diagnostics=diagnostics) == ["Observation"]
    assert diagnostics == [
        {
            "resource_type": "Patient",
            "reason": "conflicting_interactions_resource_refused",
        }
    ]


def test_complete_metadata_duplicates_are_order_independent() -> None:
    entry: dict[str, Any] = {
        "code": "search-type",
        "documentation": "synthetic",
        "extension": [{"url": "urn:source", "valueBoolean": True}],
    }
    reordered: dict[str, Any] = {
        "extension": deepcopy(entry["extension"]),
        "documentation": "synthetic",
        "code": "search-type",
    }
    for entries in permutations([{"code": "read"}, entry, reordered]):
        diagnostics: list[dict[str, str]] = []
        assert search_resources(
            capability(resource("Patient", list(entries))),
            BASE,
            diagnostics=diagnostics,
        ) == ["Patient"]
        assert diagnostics == [
            {"resource_type": "Patient", "reason": "identical_interactions_collapsed"}
        ]


def test_later_exact_duplicate_does_not_hide_an_earlier_conflict() -> None:
    a, b = (
        {"code": "search-type", "documentation": "a"},
        {"code": "search-type", "documentation": "b"},
    )
    for entries in permutations([{"code": "read"}, a, b, deepcopy(a)]):
        assert search_resources(
            capability(resource("Patient", list(entries)), resource("Observation")),
            BASE,
        ) == ["Observation"]


@pytest.mark.parametrize("clean", [[], [{"code": "read"}], [{"code": "search-type"}]])
def test_no_clean_read_search_resource_refuses(clean: list[dict[str, Any]]) -> None:
    statement = capability(
        resource(
            "Patient",
            [
                {"code": "read"},
                {"code": "search-type", "documentation": "a"},
                {"code": "search-type", "documentation": "b"},
            ],
        ),
        resource("Observation", clean),
    )
    with pytest.raises(ValueError, match="no declared readable search resource"):
        search_resources(statement, BASE)


@pytest.mark.parametrize(
    "key,value",
    [
        ("resourceType", "StructureDefinition"),
        ("fhirVersion", "5.0.0"),
        ("kind", "capability"),
        ("implementation", {"url": "https://elsewhere.test/fhir"}),
        ("implementation", None),
        ("rest", []),
        ("rest", [None]),
        ("rest", [{"mode": "client"}]),
        ("rest", [{"mode": "server", "resource": []}]),
        ("rest", [{"mode": "server", "resource": [None]}]),
    ],
)
def test_capability_guards_remain_closed(key: str, value: Any) -> None:
    statement = capability(resource("Patient"))
    statement[key] = value
    with pytest.raises(ValueError):
        search_resources(statement, BASE)


@pytest.mark.parametrize(
    "interaction", [None, "read", [None], [{}], [{"code": 1}], [{"code": False}]]
)
def test_malformed_interactions_do_not_become_clean(interaction: Any) -> None:
    bad = resource("Patient")
    bad["interaction"] = interaction
    with pytest.raises(ValueError, match="invalid interaction declaration"):
        search_resources(capability(bad, resource("Observation")), BASE)


def test_duplicate_resource_declarations_still_refuse() -> None:
    with pytest.raises(ValueError, match="ambiguous resource declaration"):
        search_resources(capability(resource("Patient"), resource("Patient")), BASE)


@pytest.mark.parametrize(
    "base",
    [
        "file:///fhir",
        "https://user:pass@example.test/fhir",
        BASE + "?x=1",
        BASE + "#fragment",
    ],
)
def test_authorized_service_binding_remains_required(base: str) -> None:
    with pytest.raises(ValueError):
        search_resources(capability(resource("Patient")), base)


def test_discovery_diagnostic_survives_unqualified_learning() -> None:
    statement = capability(
        resource(
            "Patient",
            [{"code": "read"}, {"code": "search-type"}, {"code": "search-type"}],
        )
    )
    result = learn_fhir_read(
        statement,
        base_url=BASE,
        field_paths={"id": "id"},
        probes=[],
        session=None,
        quiescence=None,
    )
    assert result.config is None
    assert result.problems
    assert result.evidence["discovery_diagnostics"] == [
        {"resource_type": "Patient", "reason": "identical_interactions_collapsed"}
    ]


def test_plain_clean_discovery_has_no_diagnostic(
    caplog: pytest.LogCaptureFixture,
) -> None:
    diagnostics: list[dict[str, str]] = []
    assert search_resources(
        capability(resource("Observation"), resource("Patient")),
        BASE,
        diagnostics=diagnostics,
    ) == ["Observation", "Patient"]
    assert diagnostics == []
    assert caplog.records == []
