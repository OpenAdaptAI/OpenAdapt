"""ensure_record: the three outcomes, and every unreadable read fails closed.

The judgment is tested on snapshots; the reader on scripted replies. The live
measurement against Medplum is in benchmark/fhir_medplum/readpath/.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from openadapt_flow.ensure_record import (
    Baseline,
    Change,
    EnsureContract,
    FhirReader,
    Unreadable,
    judge,
    read_changes,
)

BOT = "Practitioner/bot"
OTHER = "Practitioner/clinician"
T0 = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)

PARAMS = {
    "given": "Ada",
    "family": "Quill",
    "birthDate": "1984-03-07",
    "email": "Ada.Quill@example.test",
    "phone": "617-555-0142",
    "state": "Massachusetts",
}


def contract(**extra) -> EnsureContract:
    doc = {
        "resource_type": "Patient",
        "base_url": "http://fhir.test/fhir/R4",
        "self": [BOT],
        "fields": {
            "given": {"path": "name.given"},
            "family": {"path": "name.family"},
            "birthDate": {"path": "birthDate", "compare": "date"},
            "email": {"path": "telecom[system=email].value", "compare": "casefold"},
            "phone": {"path": "telecom[system=phone].value", "compare": "digits"},
            "state": {"path": "address.state", "map": {"Massachusetts": "MA"}},
        },
        "identity": [["given", "family", "birthDate"], ["email"]],
        "search": {"given": "given", "family": "family", "birthDate": "birthdate", "email": "email"},
        "settle_s": 0,
    }
    doc.update(extra)
    return EnsureContract.model_validate(doc)


def patient(pid: str, **over) -> dict:
    res = {
        "resourceType": "Patient",
        "id": pid,
        "name": [{"given": ["Ada"], "family": "Quill"}],
        "birthDate": "1984-03-07",
        "telecom": [
            {"system": "phone", "value": "(617) 555-0142"},
            {"system": "email", "value": "ada.quill@example.test"},
        ],
        "address": [{"state": "MA"}],
    }
    res.update(over)
    return res


def base(*matches: dict) -> Baseline:
    return Baseline(started=T0, matches={m["id"]: m for m in matches})


def ch(ref: str, *actions: str, by: str = BOT, **kw) -> dict[str, Change]:
    return {ref: Change(ref=ref, actions=set(actions), authors={by}, **kw)}


def test_created_once_with_every_value_is_verified():
    x = patient("x")
    r = judge(contract(), PARAMS, base(), ch("Patient/x", "C"), {"x": x}, x)
    assert (r.outcome, r.record_id) == ("created_verified", "x")


def test_existing_record_and_no_write_is_found():
    e = patient("e")
    r = judge(contract(), PARAMS, base(e), {}, {"e": e}, None)
    assert (r.outcome, r.record_id) == ("found_existing", "e")


def test_a_create_when_the_person_existed_needs_review():
    e, x = patient("e"), patient("x")
    r = judge(contract(), PARAMS, base(e), ch("Patient/x", "C"), {"e": e, "x": x}, x)
    assert r.outcome == "needs_review"


def test_two_existing_records_are_never_found():
    a, b = patient("a"), patient("b")
    r = judge(contract(), PARAMS, base(a, b), {}, {"a": a, "b": b}, None)
    assert r.outcome == "needs_review"


def test_a_wrong_value_needs_review_and_names_the_field():
    x = patient("x", birthDate="1984-07-03")
    r = judge(contract(), PARAMS, base(), ch("Patient/x", "C"), {}, x)
    assert r.outcome == "needs_review"
    assert any("birthDate" in reason for reason in r.reasons)


def test_a_collateral_write_by_the_agent_needs_review():
    x = patient("x")
    changes = {**ch("Patient/x", "C"), **ch("Observation/o", "C")}
    r = judge(contract(), PARAMS, base(), changes, {"x": x}, x)
    assert r.outcome == "needs_review"
    assert r.evidence["agent_write_types"] == ["Observation", "Patient"]


def test_a_write_on_found_needs_review():
    e = patient("e")
    r = judge(contract(), PARAMS, base(e), ch("Observation/o", "C"), {"e": e}, None)
    assert r.outcome == "needs_review"


def test_another_users_write_elsewhere_is_not_counted():
    x = patient("x")
    changes = {**ch("Patient/x", "C"), **ch("Patient/y", "U", by=OTHER)}
    r = judge(contract(), PARAMS, base(), changes, {"x": x}, x)
    assert r.outcome == "created_verified"


def test_another_users_edit_of_the_created_record_needs_review():
    x = patient("x")
    changes = {"Patient/x": Change(ref="Patient/x", actions={"C", "U"}, authors={BOT, OTHER})}
    r = judge(contract(), PARAMS, base(), changes, {"x": x}, x)
    assert r.outcome == "needs_review"


def test_an_unattributed_write_counts_as_the_agents():
    x = patient("x")
    changes = {**ch("Patient/x", "C"), "Task/t": Change(ref="Task/t", actions={"U"}, authors=set())}
    r = judge(contract(), PARAMS, base(), changes, {"x": x}, x)
    assert r.outcome == "needs_review"


def test_a_concurrent_duplicate_by_someone_else_needs_review():
    x, y = patient("x"), patient("y")
    changes = {**ch("Patient/x", "C"), **ch("Patient/y", "C", by=OTHER)}
    r = judge(contract(), PARAMS, base(), changes, {"x": x, "y": y}, x)
    assert r.outcome == "needs_review"


def test_identity_needs_a_search_parameter():
    with pytest.raises(ValueError):
        contract(search={"phone": "phone"})


# -- the reader ------------------------------------------------------------------


def bundle(*resources, total=None, next_url=None, kind="searchset"):
    body = {"resourceType": "Bundle", "type": kind, "entry": [{"resource": r} for r in resources]}
    if total is not None:
        body["total"] = total
    if next_url:
        body["link"] = [{"relation": "next", "url": next_url}]
    return body


def reader_for(routes):
    def get(url, params):
        key = url.split("/fhir/R4/", 1)[-1]
        reply = routes[key]
        if isinstance(reply, int):
            return reply, {}, None
        return 200, {}, reply

    return FhirReader(contract(change_feed={"audit": {}, "sweep_types": ["Observation"]}), get)


def audit(action, ref, who=BOT):
    return {
        "resourceType": "AuditEvent",
        "id": f"a-{ref}",
        "action": action,
        "agent": [{"who": {"reference": who}}] if who else [{}],
        "entity": [{"what": {"reference": ref}}],
    }


def versioned(res, who, when):
    return {**res, "meta": {"author": {"reference": who}, "lastUpdated": when}}


LATER = (T0 + timedelta(seconds=5)).isoformat().replace("+00:00", "Z")


def test_reader_reads_audit_history_and_sweep():
    r = reader_for(
        {
            "AuditEvent": bundle(audit("C", "Patient/x"), audit("R", "Patient/z")),
            "Observation": bundle({"resourceType": "Observation", "id": "o"}),
            "Patient/x/_history": bundle(versioned(patient("x"), BOT, LATER), kind="history"),
            "Observation/o/_history": bundle(
                versioned({"resourceType": "Observation", "id": "o"}, BOT, LATER), kind="history"
            ),
        }
    )
    changes = read_changes(
        contract(change_feed={"audit": {}, "sweep_types": ["Observation"]}), r, T0
    )
    assert set(changes) == {"Patient/x", "Observation/o"}
    assert changes["Observation/o"].unlogged and changes["Observation/o"].authors == {BOT}
    assert changes["Patient/x"].authors == {BOT}


def test_log_and_record_disagreeing_on_the_author_is_unattributed():
    r = reader_for(
        {
            "AuditEvent": bundle(audit("U", "Patient/y", who=OTHER)),
            "Observation": bundle(),
            "Patient/y/_history": bundle(versioned(patient("y"), BOT, LATER), kind="history"),
        }
    )
    changes = read_changes(contract(change_feed={"audit": {}, "sweep_types": ["Observation"]}), r, T0)
    assert changes["Patient/y"].attributed is False


@pytest.mark.parametrize(
    "routes",
    [
        {"AuditEvent": 500},
        {"AuditEvent": {"resourceType": "OperationOutcome"}},
        {"AuditEvent": bundle(total=3)},
        {"AuditEvent": bundle(next_url="http://elsewhere.test/fhir/R4/AuditEvent?p=2")},
        {"AuditEvent": bundle({"resourceType": "AuditEvent"})},
        {"AuditEvent": bundle(audit("C", None))},
    ],
    ids=["http-500", "not-a-bundle", "short-total", "other-origin", "no-id", "no-entity"],
)
def test_every_incomplete_read_is_unreadable(routes):
    r = reader_for({"Observation": bundle(), **routes})
    with pytest.raises(Unreadable):
        read_changes(contract(change_feed={"audit": {}, "sweep_types": ["Observation"]}), r, T0)
