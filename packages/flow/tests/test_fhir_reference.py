"""Schema-proven Reference identities; every record and endpoint is synthetic."""

from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from openadapt_flow.ensure_record import (
    Baseline,
    Change,
    EnsureContract,
    FhirReader,
    judge,
)
from openadapt_flow.fhir_reference import ReferenceSchema, definition_digest

BASE = "https://records.example.test/fhir/R4"
BOT = "Practitioner/verifier-agent"


def schema(resource="Observation", path="subject"):
    definition = {
        "resourceType": "StructureDefinition",
        "kind": "resource",
        "url": f"http://hl7.org/fhir/StructureDefinition/{resource}",
        "type": resource,
        "snapshot": {
            "element": [
                {"path": resource},
                {
                    "path": f"{resource}.{path}",
                    "type": [
                        {
                            "code": "Reference",
                            "targetProfile": [
                                "http://hl7.org/fhir/StructureDefinition/Patient"
                            ],
                        }
                    ],
                },
            ]
        },
    }
    return {"definition": definition, "sha256": definition_digest(definition)}


def contract(**over):
    data = {
        "resource_type": "Observation",
        "base_url": BASE,
        "self": [BOT],
        "reference_schema": schema(),
        "closed": True,
        "fields": {
            "patient": {"path": "subject.reference"},
            "display": {"path": "subject.display"},
            "note": {"path": "valueString"},
        },
        "identity": [["patient", "note"]],
        "search": {"patient": "patient"},
    }
    data.update(over)
    return EnsureContract.model_validate(data)


def record(**over):
    data = {
        "resourceType": "Observation",
        "id": "o",
        "subject": {"reference": "Patient/a", "display": "Store's presentation"},
        "valueString": "approved",
    }
    data.update(over)
    return data


def parameters(**over):
    data = {
        "patient": "Patient/a",
        "display": "Task's presentation",
        "note": "approved",
    }
    data.update(over)
    return data


class Store:
    def __init__(self):
        self.records = {
            "Patient/a": {
                "resourceType": "Patient",
                "id": "a",
                "identifier": [{"system": "urn:synthetic:registry", "value": "A"}],
            },
            "Patient/b": {
                "resourceType": "Patient",
                "id": "b",
                "identifier": [{"system": "urn:synthetic:registry", "value": "B"}],
            },
        }
        self.calls = []
        self.status = 200
        self.total = None

    def get(self, url, params=None):
        self.calls.append((url, params))
        assert url.startswith(BASE + "/"), (
            "never send a read to an external origin/base"
        )
        path = url[len(BASE) + 1 :]
        if self.status != 200:
            return self.status, {}, {}
        if params and "identifier" in params:
            system, value = params["identifier"].split("|", 1)
            rows = [
                r
                for r in self.records.values()
                if r["resourceType"] == path
                and any(
                    i["system"] == system and i["value"] == value
                    for i in r.get("identifier", [])
                )
            ]
            return (
                200,
                {},
                {
                    "resourceType": "Bundle",
                    "type": "searchset",
                    "total": self.total if self.total is not None else len(rows),
                    "entry": [{"resource": r} for r in rows],
                },
            )
        return (200, {}, self.records[path]) if path in self.records else (404, {}, {})


def verdict(c, params, resource, store):
    reader = FhirReader(c, get=store.get)
    evidence = reader.reference_evidence(c, params, [resource])
    calls = list(store.calls)
    result = judge(
        c,
        params,
        Baseline(started=datetime.now(timezone.utc), matches={}),
        {"Observation/o": Change(ref="Observation/o", actions={"C"}, authors={BOT})},
        {"o": resource},
        resource,
        references=evidence,
    )
    assert store.calls == calls, "pure judging must not perform reads"
    return result


def test_only_reference_display_differs_confirmed():
    assert (
        verdict(contract(), parameters(), record(), Store()).outcome
        == "created_verified"
    )


def test_wrong_target_with_matching_display_refuted():
    result = verdict(
        contract(),
        parameters(),
        record(subject={"reference": "Patient/b", "display": "Task's presentation"}),
        Store(),
    )
    assert result.outcome == "needs_review" and not result.evidence.get("unreadable")
    assert any("wrong or missing: patient" in r for r in result.reasons)


@pytest.mark.parametrize(
    "subject",
    [
        {"reference": "Patient/a"},
        {"reference": BASE + "/Patient/a", "display": "anything"},
    ],
)
def test_display_is_optional_and_relative_absolute_target_identity_agrees(subject):
    assert (
        verdict(contract(), parameters(), record(subject=subject), Store()).outcome
        == "created_verified"
    )


def test_identifier_only_resolves_to_same_literal_target():
    wanted = {
        "type": "Patient",
        "identifier": {"system": "urn:synthetic:registry", "value": "A"},
    }
    assert (
        verdict(contract(), parameters(patient=wanted), record(), Store()).outcome
        == "created_verified"
    )

    assert (
        verdict(
            contract(),
            parameters(),
            record(subject={"identifier": wanted["identifier"]}),
            Store(),
        ).outcome
        == "created_verified"
    )


def test_literal_and_identifier_must_agree():
    subject = {
        "reference": "Patient/a",
        "identifier": {"system": "urn:synthetic:registry", "value": "B"},
        "display": "Task's presentation",
    }
    result = verdict(contract(), parameters(), record(subject=subject), Store())
    assert result.outcome == "needs_review" and result.evidence["unreadable"]
    assert "contradict" in result.reasons[0]


def test_consistent_literal_identifier_is_verified():
    subject = {
        "reference": "Patient/a",
        "identifier": {"system": "urn:synthetic:registry", "value": "A"},
    }
    assert (
        verdict(contract(), parameters(), record(subject=subject), Store()).outcome
        == "created_verified"
    )


@pytest.mark.parametrize(
    "subject",
    [
        {"reference": "https://other.example.test/fhir/R4/Patient/a"},
        {"reference": "https://records.example.test/another/Patient/a"},
        {"reference": "Patient/a", "type": "Practitioner"},
        {"reference": "Patient/missing"},
        {"reference": "#contained"},
        {"reference": "urn:uuid:synthetic"},
        {"reference": "Patient/a?token=x"},
        {"reference": "Patient/a/_history/old"},
        {"display": "Task's presentation"},
        {"identifier": {"value": "A"}},
    ],
)
def test_unresolved_or_contradictory_identity_never_confirms(subject):
    result = verdict(contract(), parameters(), record(subject=subject), Store())
    assert result.outcome == "needs_review" and result.evidence["unreadable"]


def test_same_id_in_another_resource_type_is_not_same_target():
    store = Store()
    store.records["Practitioner/a"] = {"resourceType": "Practitioner", "id": "a"}
    result = verdict(
        contract(), parameters(), record(subject={"reference": "Practitioner/a"}), store
    )
    assert result.outcome == "needs_review" and result.evidence["unreadable"]


def test_identifier_duplicate_is_uncertain_even_when_one_literal_matches():
    store = Store()
    store.records["Patient/b"]["identifier"] = copy.deepcopy(
        store.records["Patient/a"]["identifier"]
    )
    result = verdict(
        contract(),
        parameters(
            patient={"identifier": {"system": "urn:synthetic:registry", "value": "A"}}
        ),
        record(),
        store,
    )
    assert result.outcome == "needs_review" and result.evidence["unreadable"]


def test_identifier_partial_search_is_uncertain():
    store = Store()
    store.total = 2
    result = verdict(
        contract(),
        parameters(
            patient={"identifier": {"system": "urn:synthetic:registry", "value": "A"}}
        ),
        record(),
        store,
    )
    assert result.outcome == "needs_review" and result.evidence["unreadable"]


@pytest.mark.parametrize("status", [401, 403, 500])
def test_failed_target_read_is_uncertain(status):
    store = Store()
    store.status = status
    result = verdict(contract(), parameters(), record(), store)
    assert result.outcome == "needs_review" and result.evidence["unreadable"]


def test_target_read_must_echo_the_requested_identity():
    store = Store()
    store.records["Patient/a"]["id"] = "b"
    result = verdict(contract(), parameters(), record(), store)
    assert result.outcome == "needs_review" and result.evidence["unreadable"]


def test_unknown_metadata_does_not_relax_display():
    c = contract(reference_schema=None)
    result = verdict(c, parameters(), record(), Store())
    assert result.outcome == "needs_review"
    assert any("display" in r for r in result.reasons)
    assert "reference_schema" not in c.model_dump()


def test_unrelated_field_named_display_remains_exact():
    c = contract(
        fields={
            "patient": {"path": "subject.reference"},
            "display": {"path": "display"},
            "note": {"path": "valueString"},
        }
    )
    result = verdict(c, parameters(), record(display="Different scalar"), Store())
    assert result.outcome == "needs_review" and not result.evidence.get("unreadable")


def test_display_parameter_without_target_parameter_is_uncertain():
    params = parameters()
    del params["patient"]
    assert verdict(contract(), params, record(), Store()).evidence["unreadable"]


@pytest.mark.parametrize(
    "subject",
    [
        {"reference": "Patient/a", "display": ["one", "two"]},
        {
            "reference": "Patient/a",
            "extension": [{"url": "urn:synthetic:unapproved", "valueString": "x"}],
        },
    ],
)
def test_reference_does_not_open_closed_record_members(subject):
    result = verdict(contract(), parameters(), record(subject=subject), Store())
    assert result.outcome == "needs_review" and any(
        "not given by the task" in r for r in result.reasons
    )


def test_missing_resolution_evidence_is_uncertain():
    r = record()
    result = judge(
        contract(),
        parameters(),
        Baseline(started=datetime.now(timezone.utc), matches={}),
        {"Observation/o": Change(ref="Observation/o", actions={"C"}, authors={BOT})},
        {"o": r},
        r,
    )
    assert result.evidence["unreadable"]


def test_digest_and_type_metadata_cannot_be_edited_silently():
    pin = schema()
    pin["definition"]["snapshot"]["element"][1]["type"][0]["code"] = "string"
    with pytest.raises(ValueError, match="digest"):
        ReferenceSchema.model_validate(pin)
    with pytest.raises(ValueError, match="another resource"):
        contract(reference_schema=schema("Encounter"))


def test_display_cannot_be_the_record_identity():
    with pytest.raises(ValueError, match="display cannot"):
        contract(identity=[["display"]], search={"display": "patient"})


def test_metadata_without_an_independent_identity_binding_refuses():
    with pytest.raises(ValueError, match="bound target identity"):
        contract(
            fields={"display": {"path": "subject.display"}},
            identity=[["display"]],
            search={"display": "patient"},
        )


def test_legacy_contract_canonical_digest_and_round_trip_are_unchanged():
    import hashlib
    import json

    doc = {
        "resource_type": "Patient",
        "base_url": BASE,
        "self": [BOT],
        "fields": {"name": {"path": "name.family"}},
        "identity": [["name"]],
        "search": {"name": "family"},
    }
    c = EnsureContract.model_validate(doc)
    raw = c.model_dump(mode="json", by_alias=True)
    # Recorded from the unmodified model at 800f450047c3195bb28a5e6b6b8f862a5e21a16f.
    digest = hashlib.sha256(
        json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert digest == "5847c5c359a0d8836f3d5b7c9e63c0cee3c088ee46646b833a915daaa3ff7c30"
    assert "reference_schema" not in raw
    assert (
        EnsureContract.model_validate(raw).model_dump(mode="json", by_alias=True) == raw
    )


def test_reference_evidence_does_not_carry_unrelated_target_fields():
    store = Store()
    store.records["Patient/a"]["unrelated"] = "SYNTHETIC-UNRELATED-TARGET-DATA"
    c = contract()
    evidence = FhirReader(c, get=store.get).reference_evidence(
        c, parameters(), [record()]
    )
    assert "SYNTHETIC-UNRELATED-TARGET-DATA" not in repr(evidence)
    assert verdict(c, parameters(), record(), store).outcome == "created_verified"


def test_unsupported_identifier_metadata_stays_uncertain_and_out_of_evidence():
    wanted = {
        "identifier": {
            "system": "urn:synthetic:registry",
            "value": "A",
            "assigner": {"display": "SYNTHETIC-UNRELATED-ASSIGNER"},
        }
    }
    c = contract()
    evidence = FhirReader(c, get=Store().get).reference_evidence(
        c, parameters(patient=wanted), [record()]
    )
    assert "SYNTHETIC-UNRELATED-ASSIGNER" not in repr(evidence)
    assert verdict(c, parameters(patient=wanted), record(), Store()).evidence[
        "unreadable"
    ]


def test_reference_uri_credentials_and_queries_are_not_persisted():
    c = contract()
    for uri in (
        "https://synthetic-user:PRIVATE-MARKER@records.example.test/fhir/R4/Patient/a",
        "Patient/a?token=PRIVATE-MARKER",
    ):
        resource = record(subject={"reference": uri})
        evidence = FhirReader(c, get=Store().get).reference_evidence(
            c, parameters(), [resource]
        )
        assert "PRIVATE-MARKER" not in repr(evidence)
        assert verdict(c, parameters(), resource, Store()).evidence["unreadable"]


@pytest.mark.parametrize(
    "earlier_target,expected",
    [("Patient/a", "created_verified"), ("Patient/b", "needs_review")],
)
def test_reference_identity_remains_checked_in_every_corrected_version(
    earlier_target, expected
):
    from datetime import timedelta

    c = contract(settle_s=1, operations={"correction": {"between": "consistent"}})
    params = parameters()
    first = record(
        subject={"reference": earlier_target, "display": "Earlier presentation"}
    )
    final = record()
    store = Store()
    evidence = FhirReader(c, get=store.get).reference_evidence(
        c, params, [first, final]
    )
    now = datetime.now(timezone.utc)
    first["meta"] = {"versionId": "1", "lastUpdated": now.isoformat()}
    final["meta"] = {
        "versionId": "2",
        "lastUpdated": (now + timedelta(seconds=1)).isoformat(),
    }
    change = Change(
        ref="Observation/o",
        actions={"C", "U"},
        authors={BOT},
        versions=2,
        bodies=[first, final],
        events=[
            (now, "C", frozenset({BOT})),
            (now + timedelta(seconds=1), "U", frozenset({BOT})),
        ],
    )
    result = judge(
        c,
        params,
        Baseline(started=now, matches={}),
        {"Observation/o": change},
        {"o": final},
        final,
        references=evidence,
    )
    assert result.outcome == expected
    assert not result.evidence.get("unreadable")


@pytest.mark.parametrize(
    "target,read_version,expected,reason",
    [
        ("Patient/a", "2", "created_verified", None),
        ("Patient/a", "1", "needs_review", "not the last version"),
        ("Patient/b", "2", "needs_review", "wrong or missing: patient"),
        ("Patient/a", None, "needs_review", "no versionId"),
    ],
)
def test_reference_identity_and_last_written_version_are_both_required(
    target, read_version, expected, reason
):
    now = datetime.now(timezone.utc)
    written = record(
        subject={"reference": target, "display": "Task's presentation"},
        meta={"versionId": "2", "lastUpdated": now.isoformat()},
    )
    read_back = copy.deepcopy(written)
    if read_version is None:
        del read_back["meta"]["versionId"]
    else:
        read_back["meta"]["versionId"] = read_version
    c = contract()
    params = parameters()
    store = Store()
    evidence = FhirReader(c, get=store.get).reference_evidence(
        c, params, [written, read_back]
    )
    calls = list(store.calls)
    change = Change(
        ref="Observation/o",
        actions={"C"},
        authors={BOT},
        versions=1,
        bodies=[written],
        events=[(now, "C", frozenset({BOT}))],
    )
    result = judge(
        c,
        params,
        Baseline(started=now, matches={}),
        {"Observation/o": change},
        {"o": read_back},
        read_back,
        references=evidence,
    )
    assert result.outcome == expected
    assert not result.evidence.get("unreadable")
    assert store.calls == calls, "pure judging must not perform reads"
    if reason:
        assert any(reason in item for item in result.reasons)
    else:
        assert result.evidence["versions"] == [{"version": "2", "author": BOT}]


class SearchStore(Store):
    def __init__(self, resources=(), *, strict=False):
        super().__init__()
        self.resources = list(resources)
        self.strict = strict
        self.queries = []
        for version in ("1", "2"):
            target = copy.deepcopy(self.records["Patient/a"])
            target["meta"] = {"versionId": version}
            self.records[f"Patient/a/_history/{version}"] = target

    def get(self, url, params=None):
        if url == BASE + "/Observation":
            self.queries.append(dict(params or {}))
            rows = self.resources
            if self.strict and (params or {}).get("patient") != "Patient/a":
                rows = []
            return 200, {}, {
                "resourceType": "Bundle",
                "type": "searchset",
                "total": len(rows),
                "entry": [{"resource": r} for r in rows],
            }
        return super().get(url, params)


@pytest.mark.parametrize(
    "wanted,stored",
    [("Patient/a", "Patient/a/_history/1"), ("Patient/a/_history/1", "Patient/a")],
)
def test_versioned_reference_cannot_hide_existing_record_or_allow_duplicate(
    wanted, stored
):
    from openadapt_flow.ensure_record import find_matches

    c = contract()
    params = parameters(patient=wanted)
    existing = record(subject={"reference": stored, "display": "Other presentation"})
    store = SearchStore([existing])
    reader = FhirReader(c, get=store.get)
    matches, candidates = find_matches(c, reader, params)
    assert candidates == 1 and set(matches) == {"o"}
    baseline = Baseline(started=datetime.now(timezone.utc), matches=matches)
    created = record(id="new", subject={"reference": wanted})
    store.resources.append(created)
    after, _ = find_matches(c, reader, params)
    assert set(after) == {"o", "new"}
    evidence = reader.reference_evidence(c, params, list(after.values()))
    result = judge(
        c,
        params,
        baseline,
        {"Observation/new": Change(ref="Observation/new", actions={"C"}, authors={BOT})},
        after,
        created,
        references=evidence,
    )
    assert result.outcome == "needs_review"
    assert result.evidence["before_matches"] == 1
    assert result.evidence["after_matches"] == 2
    assert not result.evidence.get("unreadable")


@pytest.mark.parametrize(
    "wanted",
    [
        "Patient/a",
        BASE + "/Patient/a",
        "Patient/a/_history/1",
        BASE + "/Patient/a/_history/1",
    ],
)
def test_reference_search_uses_local_logical_type_id(wanted):
    from openadapt_flow.ensure_record import find_matches

    c = contract()
    store = SearchStore([record()], strict=True)
    matches, count = find_matches(
        c, FhirReader(c, get=store.get), parameters(patient=wanted)
    )
    assert set(matches) == {"o"} and count == 1
    assert store.queries == [{"patient": "Patient/a", "_count": "200"}]


def test_missing_expected_reference_search_match_is_unreadable():
    from openadapt_flow.ensure_record import Unreadable, find_matches

    c = contract()
    reader = FhirReader(c, get=SearchStore().get)
    with pytest.raises(Unreadable, match="known matching record"):
        find_matches(c, reader, parameters(), expected_ids=["o"])


def test_initial_reference_search_may_be_empty_without_expected_record():
    from openadapt_flow.ensure_record import find_matches

    c = contract()
    assert find_matches(c, FhirReader(c, get=SearchStore().get), parameters()) == ({}, 0)


def test_logical_reference_identity_retains_verified_version_separately():
    c = contract()
    params = parameters(patient="Patient/a/_history/1")
    evidence = FhirReader(c, get=SearchStore().get).reference_evidence(
        c, params, [record()]
    )
    assert evidence.identity(params["patient"], ("Patient",)) == BASE + "/Patient/a"
    assert evidence.identity("Patient/a", ("Patient",)) == BASE + "/Patient/a"
    assert evidence.version(params["patient"], ("Patient",)) == "1"
    assert evidence.version("Patient/a", ("Patient",)) is None


def test_requested_reference_version_is_still_checked_outside_person_identity():
    from openadapt_flow.ensure_record import is_person, wrong_fields

    c = contract()
    params = parameters(patient="Patient/a/_history/1")
    resource = record(subject={"reference": "Patient/a/_history/2"})
    evidence = FhirReader(c, get=SearchStore().get).reference_evidence(
        c, params, [resource]
    )
    assert is_person(c, resource, params, evidence)
    assert wrong_fields(c, resource, params, evidence) == ["patient"]


def test_version_target_read_must_still_echo_requested_version():
    store = SearchStore()
    store.records["Patient/a/_history/1"]["meta"]["versionId"] = "2"
    result = verdict(contract(), parameters(patient="Patient/a/_history/1"), record(), store)
    assert result.outcome == "needs_review" and result.evidence["unreadable"]


def test_closed_typed_reference_does_not_certify_its_display_text():
    resource = record(
        subject={"reference": "Patient/a", "display": "An unrelated person's label"}
    )
    assert (
        verdict(contract(), parameters(), resource, Store()).outcome == "created_verified"
    )


def test_matching_display_does_not_make_a_different_target_an_existing_person():
    from openadapt_flow.ensure_record import find_matches

    c = contract()
    resource = record(subject={"reference": "Patient/b", "display": "Task's presentation"})
    store = SearchStore([resource])
    assert find_matches(c, FhirReader(c, get=store.get), parameters()) == ({}, 1)


@pytest.mark.parametrize("created", [False, True])
def test_verification_refuses_search_omission_of_fresh_known_match(
    monkeypatch, created
):
    from openadapt_flow import ensure_record

    c = contract(change_feed={"audit": None, "sweep_types": ["Observation"]})
    resource = record()
    store = SearchStore()
    store.records["Observation/o"] = resource
    now = datetime.now(timezone.utc)
    changes = (
        {"Observation/o": Change(ref="Observation/o", actions={"C"}, authors={BOT})}
        if created
        else {}
    )
    monkeypatch.setattr(ensure_record, "read_changes", lambda *args: changes)
    monkeypatch.setattr(FhirReader, "server_time", lambda self: now)
    result = ensure_record.verify_ensure(
        c,
        parameters(),
        Baseline(started=now, matches={} if created else {"o": resource}),
        reader=FhirReader(c, get=store.get),
        sleep=lambda seconds: None,
    )
    assert result.outcome == "needs_review" and result.evidence["unreadable"]
    assert any("known matching record" in reason for reason in result.reasons)
