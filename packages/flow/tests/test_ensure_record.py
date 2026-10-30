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
        "search": {
            "given": "given",
            "family": "family",
            "birthDate": "birthdate",
            "email": "email",
        },
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
    changes = {
        "Patient/x": Change(ref="Patient/x", actions={"C", "U"}, authors={BOT, OTHER})
    }
    r = judge(contract(), PARAMS, base(), changes, {"x": x}, x)
    assert r.outcome == "needs_review"


def test_an_unattributed_write_counts_as_the_agents():
    x = patient("x")
    changes = {
        **ch("Patient/x", "C"),
        "Task/t": Change(ref="Task/t", actions={"U"}, authors=set()),
    }
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
    body = {
        "resourceType": "Bundle",
        "type": kind,
        "entry": [{"resource": r} for r in resources],
    }
    if total is not None:
        body["total"] = total
    if next_url:
        body["link"] = [{"relation": "next", "url": next_url}]
    return body


def with_outcome(body, severity):
    body["entry"] = list(body.get("entry") or []) + [
        {
            "resource": {
                "resourceType": "OperationOutcome",
                "issue": [{"severity": severity, "code": "processing"}],
            },
            "search": {"mode": "outcome"},
        }
    ]
    return body


def reader_for(routes):
    def get(url, params):
        key = url.split("/fhir/R4/", 1)[-1]
        reply = routes[key]
        if isinstance(reply, int):
            return reply, {}, None
        return 200, {}, reply

    return FhirReader(
        contract(change_feed={"audit": {}, "sweep_types": ["Observation"]}), get
    )


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
            "Patient/x/_history": bundle(
                versioned(patient("x"), BOT, LATER), kind="history"
            ),
            "Observation/o/_history": bundle(
                versioned({"resourceType": "Observation", "id": "o"}, BOT, LATER),
                kind="history",
            ),
        }
    )
    changes = read_changes(
        contract(change_feed={"audit": {}, "sweep_types": ["Observation"]}), r, T0
    )
    assert set(changes) == {"Patient/x", "Observation/o"}
    assert changes["Observation/o"].unlogged and changes["Observation/o"].authors == {
        BOT
    }
    assert changes["Patient/x"].authors == {BOT}


def test_log_and_record_disagreeing_on_the_author_is_unattributed():
    r = reader_for(
        {
            "AuditEvent": bundle(audit("U", "Patient/y", who=OTHER)),
            "Observation": bundle(),
            "Patient/y/_history": bundle(
                versioned(patient("y"), BOT, LATER), kind="history"
            ),
        }
    )
    changes = read_changes(
        contract(change_feed={"audit": {}, "sweep_types": ["Observation"]}), r, T0
    )
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
        {"AuditEvent": with_outcome(bundle(), "error")},
    ],
    ids=[
        "http-500",
        "not-a-bundle",
        "short-total",
        "other-origin",
        "no-id",
        "no-entity",
        "error-outcome",
    ],
)
def test_every_incomplete_read_is_unreadable(routes):
    r = reader_for({"Observation": bundle(), **routes})
    with pytest.raises(Unreadable):
        read_changes(
            contract(change_feed={"audit": {}, "sweep_types": ["Observation"]}), r, T0
        )


def test_a_warning_outcome_is_a_message_not_an_unreadable_search():
    """Epic answers an empty search with a warning outcome entry."""

    r = reader_for(
        {"Observation": bundle(), "AuditEvent": with_outcome(bundle(), "warning")}
    )
    changes = read_changes(
        contract(change_feed={"audit": {}, "sweep_types": ["Observation"]}), r, T0
    )
    assert changes == {}


def test_a_server_without_history_authors_relies_on_the_audit_trail():
    feed = {"audit": {}, "sweep_types": ["Observation"], "history_author": False}
    r = reader_for(
        {
            "AuditEvent": bundle(audit("C", "Patient/x")),
            "Observation": bundle(),
            "Patient/x/_history": bundle(
                {**patient("x"), "meta": {"lastUpdated": LATER}}, kind="history"
            ),
        }
    )
    changes = read_changes(contract(change_feed=feed), r, T0)
    assert changes["Patient/x"].attributed and changes["Patient/x"].authors == {BOT}


def test_without_history_authors_an_unlogged_write_is_the_agents():
    feed = {"audit": {}, "sweep_types": ["Observation"], "history_author": False}
    obs = {"resourceType": "Observation", "id": "o", "meta": {"lastUpdated": LATER}}
    r = reader_for(
        {
            "AuditEvent": bundle(),
            "Observation": bundle(obs),
            "Observation/o/_history": bundle(obs, kind="history"),
        }
    )
    changes = read_changes(contract(change_feed=feed), r, T0)
    x = patient("x")
    result = judge(
        contract(change_feed=feed),
        PARAMS,
        base(),
        {**ch("Patient/x", "C"), **changes},
        {"x": x},
        x,
    )
    assert result.outcome == "needs_review"


def test_an_element_the_task_did_not_give_needs_review():
    x = patient("x", maritalStatus={"text": "M"})
    x["name"][0]["given"].append("Mae")
    r = judge(contract(closed=True), PARAMS, base(), ch("Patient/x", "C"), {"x": x}, x)
    assert r.outcome == "needs_review"
    assert any("maritalStatus.text" in m and "name.given" in m for m in r.reasons)


def test_a_closed_record_with_exactly_the_task_values_is_verified():
    x = patient("x")
    r = judge(
        contract(closed=True, allow=["address.use"]),
        PARAMS,
        base(),
        ch("Patient/x", "C"),
        {"x": x},
        x,
    )
    assert r.outcome == "created_verified"


def test_another_users_edit_of_the_found_record_is_still_found():
    e = patient("e", birthDate="1999-01-01")
    changes = ch("Patient/e", "U", by=OTHER)
    r = judge(contract(), PARAMS, base(patient("e")), changes, {}, None, existing_now=e)
    assert (r.outcome, r.record_id) == ("found_existing", "e")


def test_a_found_record_that_is_gone_needs_review():
    r = judge(
        contract(),
        PARAMS,
        base(patient("e")),
        ch("Patient/e", "D", by=OTHER),
        {},
        None,
        None,
    )
    assert r.outcome == "needs_review"


def _live_reader(audit_replies, patients):
    """A server whose audit trail answers each read in turn (the last repeats)."""

    from openadapt_flow.ensure_record import verify_ensure  # noqa: F401

    calls = {"audit": 0}

    def get(url, params):
        key = url.split("/fhir/R4/", 1)[-1]
        if key == "metadata":
            return (
                200,
                {"Date": "Wed, 28 Oct 2026 12:00:10 GMT"},
                {"resourceType": "CapabilityStatement"},
            )
        if key == "AuditEvent":
            reply = audit_replies[min(calls["audit"], len(audit_replies) - 1)]
            calls["audit"] += 1
            return 200, {}, reply
        if key.endswith("/_history"):
            return 200, {}, bundle(kind="history")
        if key.startswith("Patient/"):
            return 200, {}, patients[key.split("/")[1]]
        return 200, {}, bundle(*patients.values())

    return FhirReader(contract(change_feed={"audit": {}, "history": True}), get)


def test_an_audit_event_that_arrives_late_is_still_read():
    from openadapt_flow.ensure_record import verify_ensure

    x = patient("x")
    late = [
        bundle(audit("C", "Patient/x")),
        bundle(audit("C", "Patient/x"), audit("C", "Observation/o")),
    ]
    r = verify_ensure(
        contract(change_feed={"audit": {}}),
        PARAMS,
        base(),
        reader=_live_reader(late, {"x": x}),
        sleep=lambda s: None,
    )
    assert r.outcome == "needs_review"
    assert r.evidence["agent_write_types"] == ["Observation", "Patient"]


def test_a_change_feed_that_never_settles_is_unreadable():
    from openadapt_flow.ensure_record import verify_ensure

    x = patient("x")
    growing = [
        bundle(
            *(audit("C", f"Observation/o{i}") for i in range(n)),
            audit("C", "Patient/x"),
        )
        for n in range(9)
    ]
    r = verify_ensure(
        contract(change_feed={"audit": {}}),
        PARAMS,
        base(),
        reader=_live_reader(growing, {"x": x}),
        sleep=lambda s: None,
    )
    assert r.outcome == "needs_review" and r.evidence.get("unreadable")
    assert "still changing" in r.reasons[0]


def _found_reader(audit_trail_logs_reads: bool):
    e = patient("e")

    def get(url, params):
        key = url.split("/fhir/R4/", 1)[-1]
        if key == "metadata":
            return (
                200,
                {"Date": "Wed, 28 Oct 2026 12:00:10 GMT"},
                {"resourceType": "CapabilityStatement"},
            )
        if key == "AuditEvent":
            if (params or {}).get("action") == "R" and audit_trail_logs_reads:
                return 200, {}, bundle(audit("R", "Patient/e"))
            return 200, {}, bundle()
        if key == "Patient/e":
            return 200, {}, e
        return 200, {}, bundle(e)

    return FhirReader(contract(), get)


@pytest.mark.parametrize(
    "logging,outcome", [(True, "found_existing"), (False, "needs_review")]
)
def test_found_needs_the_trail_to_log_the_verifiers_own_read(logging, outcome):
    from openadapt_flow.ensure_record import verify_ensure

    c = contract(change_feed={"audit": {"heartbeat": True}})
    r = verify_ensure(
        c,
        PARAMS,
        base(patient("e")),
        reader=_found_reader(logging),
        sleep=lambda s: None,
    )
    assert r.outcome == outcome


def test_a_record_holding_the_printed_state_instead_of_the_code_is_wrong():
    # The map turns the task's value into the stored form; it must not also
    # turn a wrongly stored "Massachusetts" into "MA" (blind round 1 miss).
    x = patient("x", address=[{"state": "Massachusetts"}])
    r = judge(contract(), PARAMS, base(), ch("Patient/x", "C"), {"x": x}, x)
    assert r.outcome == "needs_review"
    assert any("state" in reason for reason in r.reasons)


@pytest.mark.parametrize(
    "active,outcome",
    [(True, "created_verified"), (False, "needs_review"), ("true", "needs_review")],
)
def test_an_allowed_element_must_hold_the_allowed_value(active, outcome):
    x = patient("x", active=active)
    r = judge(
        contract(closed=True, allow=["active=true"]),
        PARAMS,
        base(),
        ch("Patient/x", "C"),
        {"x": x},
        x,
    )
    assert r.outcome == outcome


# -- adversarial review of #1640 -------------------------------------------------


def test_found_with_a_new_duplicate_now_needs_review():
    e, d = patient("e"), patient("d")
    r = judge(
        contract(),
        PARAMS,
        base(e),
        ch("Patient/d", "C", by=OTHER),
        {"e": e, "d": d},
        None,
        existing_now=e,
    )
    assert r.outcome == "needs_review"


def test_the_person_under_a_second_name_or_email_is_found():
    from openadapt_flow.ensure_record import is_person

    maiden = patient(
        "m",
        name=[
            {"use": "old", "given": ["Ada"], "family": "Byron"},
            {"given": ["Ada"], "family": "Quill"},
        ],
    )
    second_email = patient(
        "s",
        name=[{"given": ["Augusta"], "family": "King"}],
        telecom=[
            {"system": "email", "value": "work@example.test"},
            {"system": "email", "value": "ada.quill@example.test"},
        ],
    )
    assert is_person(contract(), maiden, PARAMS) and is_person(
        contract(), second_email, PARAMS
    )


def test_a_rule_whose_values_the_task_lacks_is_skipped():
    from openadapt_flow.ensure_record import is_person

    params = {k: v for k, v in PARAMS.items() if k != "email"}
    assert is_person(contract(), patient("x"), params)


def test_an_audit_event_with_no_action_is_unreadable():
    event = audit("C", "Patient/x")
    del event["action"]
    r = reader_for({"AuditEvent": bundle(event), "Observation": bundle()})
    with pytest.raises(Unreadable):
        read_changes(
            contract(change_feed={"audit": {}, "sweep_types": ["Observation"]}), r, T0
        )


def test_an_operation_the_agent_runs_counts_as_its_write():
    r = reader_for(
        {
            "AuditEvent": bundle(audit("C", "Patient/x"), audit("E", "Bot/b")),
            "Observation": bundle(),
            "Patient/x/_history": bundle(kind="history"),
            "Bot/b/_history": bundle(kind="history"),
        }
    )
    feed = {"audit": {}, "sweep_types": ["Observation"], "history_author": False}
    changes = read_changes(contract(change_feed=feed), r, T0)
    x = patient("x")
    assert (
        judge(contract(), PARAMS, base(), changes, {"x": x}, x).outcome
        == "needs_review"
    )


def test_an_absolute_actor_reference_is_still_the_agent():
    changes = ch("Patient/x", "C", by="http://fhir.test/fhir/R4/" + BOT)
    x = patient("x")
    from openadapt_flow.ensure_record import _ref

    changes["Patient/x"].authors = {_ref(a) for a in changes["Patient/x"].authors}
    assert (
        judge(contract(), PARAMS, base(), changes, {"x": x}, x).outcome
        == "created_verified"
    )


def test_a_closed_record_refuses_a_value_the_task_did_not_give_and_a_security_label():
    params = {k: v for k, v in PARAMS.items() if k != "phone"}
    x = patient("x")  # still holds a phone
    r = judge(contract(closed=True), params, base(), ch("Patient/x", "C"), {"x": x}, x)
    assert r.outcome == "needs_review"
    y = patient("y", meta={"security": [{"code": "R"}], "versionId": "1"})
    r = judge(contract(closed=True), PARAMS, base(), ch("Patient/y", "C"), {"y": y}, y)
    assert r.outcome == "needs_review" and any("meta.security" in m for m in r.reasons)


def test_a_timestamp_with_no_zone_is_unreadable_not_a_crash():
    r = reader_for(
        {
            "AuditEvent": bundle(audit("C", "Patient/x")),
            "Observation": bundle(),
            "Patient/x/_history": bundle(
                {**patient("x"), "meta": {"lastUpdated": "2026-10-28T12:00:05"}},
                kind="history",
            ),
        }
    )
    with pytest.raises(Unreadable):
        read_changes(
            contract(change_feed={"audit": {}, "sweep_types": ["Observation"]}), r, T0
        )


def test_a_write_that_lands_after_the_window_needs_review():
    from openadapt_flow.ensure_record import verify_ensure

    x = patient("x")
    inside = bundle(audit("C", "Patient/x"))
    after = bundle(audit("C", "Patient/x"), audit("C", "Observation/late"))

    def get(url, params):
        key = url.split("/fhir/R4/", 1)[-1]
        if key == "metadata":
            return (
                200,
                {"Date": "Wed, 28 Oct 2026 12:00:10 GMT"},
                {"resourceType": "CapabilityStatement"},
            )
        if key == "AuditEvent":
            bounded = any(
                str(v).startswith("le") for v in (params or {}).get("date", [])
            )
            return 200, {}, inside if bounded else after
        if key.endswith("/_history"):
            return 200, {}, bundle(kind="history")
        if key == "Patient/x":
            return 200, {}, x
        return 200, {}, bundle(x)

    c = contract(change_feed={"audit": {}})
    r = verify_ensure(
        c, PARAMS, base(), reader=FhirReader(c, get), sleep=lambda s: None
    )
    assert r.outcome == "needs_review" and "after the window" in r.reasons[0]


def test_instant_compare_is_one_moment_to_the_second():
    from openadapt_flow.ensure_record import FieldRule, normalize

    rule = FieldRule(path="start", compare="instant")
    assert normalize("2026-11-17T09:00:00-05:00", rule) == normalize(
        "2026-11-17T14:00:00.000Z", rule
    )
    assert normalize("2026-11-17T14:00:00Z", rule) != normalize(
        "2026-11-17T15:00:00Z", rule
    )
    # A time with no zone names no moment: it never matches.
    assert normalize("2026-11-17T14:00:00", rule) is None
    assert normalize("not a time", rule) is None


def test_a_prefix_filter_is_one_leaf_in_a_closed_record():
    from openadapt_flow.ensure_record import (
        EnsureContract,
        extra_elements,
        wrong_fields,
    )

    c = EnsureContract.model_validate(
        {
            "resource_type": "Appointment",
            "base_url": "http://127.0.0.1:1/fhir",
            "self": ["Practitioner/bot"],
            "fields": {
                "patient": {
                    "path": "participant[actor.reference^=Patient/].actor.reference"
                },
                "doctor": {
                    "path": "participant[actor.reference^=Practitioner/].actor.reference"
                },
            },
            "identity": [["patient"]],
            "search": {"patient": "patient"},
            "closed": True,
        }
    )
    params = {"patient": "Patient/p1", "doctor": "Practitioner/d1"}
    res = {
        "resourceType": "Appointment",
        "id": "a",
        "participant": [
            {"actor": {"reference": "Practitioner/d1"}},
            {"actor": {"reference": "Patient/p1"}},
        ],
    }
    assert wrong_fields(c, res, params) == []
    assert extra_elements(c, res, params) == []
    res["participant"].append({"actor": {"reference": "Patient/p2"}})
    assert extra_elements(c, res, params) == ["participant.actor.reference"]


def test_an_identity_value_without_a_search_parameter_is_compared_not_searched():
    from openadapt_flow.ensure_record import EnsureContract, find_matches

    c = EnsureContract.model_validate(
        {
            "resource_type": "AllergyIntolerance",
            "base_url": "http://127.0.0.1:1/fhir",
            "self": ["Practitioner/bot"],
            "fields": {
                "patient": {"path": "patient.reference"},
                "code": {"path": "code.coding.code"},
            },
            "identity": [["patient", "code"]],
            "search": {"patient": "patient"},
        }
    )
    asked = []

    class Reader:
        def resources(self, rtype, query):
            asked.append(dict(query))
            return [
                {
                    "id": "a1",
                    "patient": {"reference": "Patient/p1"},
                    "code": {"coding": [{"code": "91936005"}]},
                },
                {
                    "id": "a2",
                    "patient": {"reference": "Patient/p1"},
                    "code": {"coding": [{"code": "300916003"}]},
                },
            ]

    matches, _ = find_matches(
        c, Reader(), {"patient": "Patient/p1", "code": "91936005"}
    )
    assert set(matches) == {"a1"}
    assert all(set(q) == {"patient", "_count"} for q in asked)


# -- void records: a record that asserts nothing is never the person -------------

VAX_PARAMS = {"patient": "Patient/p1", "code": "141", "date": "2025-10-01"}


def vax_contract(**extra) -> EnsureContract:
    doc = {
        "resource_type": "Immunization",
        "base_url": "http://fhir.test/fhir/R4",
        "self": [BOT],
        "fields": {
            "patient": {"path": "patient.reference"},
            "code": {"path": "vaccineCode.coding.code"},
            "date": {"path": "occurrenceDateTime", "compare": "date"},
        },
        "identity": [["patient", "code", "date"]],
        "search": {"patient": "patient", "code": "vaccine-code", "date": "date"},
        "closed": True,
        "allow": ['status="completed"'],
        "void": ['status="entered-in-error"'],
        "settle_s": 0,
    }
    doc.update(extra)
    return EnsureContract.model_validate(doc)


def vax(iid: str, status: str = "completed") -> dict:
    return {
        "resourceType": "Immunization",
        "id": iid,
        "status": status,
        "patient": {"reference": "Patient/p1"},
        "vaccineCode": {"coding": [{"code": "141"}]},
        "occurrenceDateTime": "2025-10-01",
    }


def test_a_void_lookalike_is_not_the_person():
    from openadapt_flow.ensure_record import is_person, is_void

    c = vax_contract()
    assert is_void(c, vax("v", "entered-in-error"))
    assert not is_person(c, vax("v", "entered-in-error"), VAX_PARAMS)
    assert is_person(c, vax("x"), VAX_PARAMS)


def test_a_create_beside_a_void_lookalike_is_verified():
    # The void record was there before and is still there; neither counts.
    x = vax("x")
    r = judge(
        vax_contract(), VAX_PARAMS, base(), ch("Immunization/x", "C"), {"x": x}, x
    )
    assert (r.outcome, r.record_id) == ("created_verified", "x")


def test_without_void_the_same_lookalike_stops():
    from openadapt_flow.ensure_record import is_person

    c = vax_contract(void=[])
    assert is_person(c, vax("v", "entered-in-error"), VAX_PARAMS)


def test_a_created_record_that_is_void_needs_review():
    x = vax("x", "entered-in-error")
    r = judge(
        vax_contract(allow=[]), VAX_PARAMS, base(), ch("Immunization/x", "C"), {}, x
    )
    assert r.outcome == "needs_review"
    assert any("asserts nothing" in reason for reason in r.reasons)


def test_a_status_void_does_not_name_still_matches():
    # A deny list: a value it misses stops for review, never hides a duplicate.
    from openadapt_flow.ensure_record import is_person

    assert is_person(vax_contract(), vax("n", "not-done"), VAX_PARAMS)
    e, x = vax("e", "not-done"), vax("x")
    r = judge(
        vax_contract(),
        VAX_PARAMS,
        base(e),
        ch("Immunization/x", "C"),
        {"e": e, "x": x},
        x,
    )
    assert r.outcome == "needs_review"


def test_void_compares_json_values_in_lists():
    from openadapt_flow.ensure_record import is_void

    c = vax_contract(void=['meta.tag.code="void"'])
    tagged = dict(vax("t"), meta={"tag": [{"code": "keep"}, {"code": "void"}]})
    assert is_void(c, tagged)
    assert not is_void(c, dict(vax("t"), meta={"tag": [{"code": "keep"}]}))
    assert not is_void(vax_contract(void=["status=1"]), vax("s"))


@pytest.mark.parametrize(
    "entry",
    [
        "status",
        '="x"',
        "status=entered-in-error",
        'patient.reference="Patient/p1"',
        'status ="entered-in-error"',
        'status[x="y"="z"',
        'status=["entered-in-error"]',
        'patient[type=Patient].reference="Patient/p1"',
    ],
)
def test_a_void_entry_must_be_a_path_and_a_json_value_off_identity(entry):
    with pytest.raises(ValueError):
        vax_contract(void=[entry])


def test_find_matches_leaves_out_void_records():
    from openadapt_flow.ensure_record import find_matches

    rows = [vax("v", "entered-in-error"), vax("x")]

    def get(url, params):
        entry = [{"resource": r, "search": {"mode": "match"}} for r in rows]
        return 200, {}, {"resourceType": "Bundle", "type": "searchset", "entry": entry}

    c = vax_contract()
    matches, seen = find_matches(c, FhirReader(c, get=get), VAX_PARAMS)
    assert set(matches) == {"x"} and seen == 2


# -- the adversarial review's findings (REVIEW_VOID.md) ---------------------------


def test_review_1_an_existing_record_voided_in_the_window_is_not_found():
    a, now = vax("a"), vax("a", "entered-in-error")
    changes = ch("Immunization/a", "U", by=OTHER)
    r = judge(vax_contract(), VAX_PARAMS, base(a), changes, {}, None, now)
    assert r.outcome == "needs_review"
    assert any("asserts nothing" in reason for reason in r.reasons)


def test_review_2_void_values_compare_as_json_not_python_equality():
    from openadapt_flow.ensure_record import is_void

    assert not is_void(
        vax_contract(void=["primarySource=true"]), vax("s") | {"primarySource": 1}
    )
    assert not is_void(
        vax_contract(void=["doseQuantity.value=0"]),
        vax("s") | {"doseQuantity": {"value": False}},
    )
    assert is_void(
        vax_contract(void=["primarySource=true"]), vax("s") | {"primarySource": True}
    )


def test_review_4_a_void_path_may_filter_a_list():
    from openadapt_flow.ensure_record import is_void

    c = vax_contract(void=['identifier[system=urn:x].value="void"'])
    hit = vax("i") | {
        "identifier": [
            {"system": "urn:y", "value": "void"},
            {"system": "urn:x", "value": "void"},
        ]
    }
    miss = vax("i") | {"identifier": [{"system": "urn:y", "value": "void"}]}
    assert is_void(c, hit) and not is_void(c, miss)


def test_review_6_an_unvalidated_bad_entry_reads_as_unreadable():
    from openadapt_flow.ensure_record import is_void

    c = vax_contract().model_copy(update={"void": ["status=entered-in-error"]})
    with pytest.raises(Unreadable):
        is_void(c, vax("x"))


# -- the agent's own corrections ------------------------------------------------------

READER = "ClientApplication/someone"


def _t(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def cc(**extra) -> EnsureContract:
    """A contract that may accept corrections (they need settle_s >= 1)."""

    return contract(settle_s=1, **extra)


def fixed(*extra_events, reads=(), versions=None, bodies=None, ref="Patient/x"):
    """The agent created ``ref`` at +1 s and updated it at +3 s."""

    events = [(_t(1), "C", frozenset({BOT})), (_t(3), "U", frozenset({BOT}))]
    events += list(extra_events)
    n = len(events) if versions is None else versions
    return {
        ref: Change(
            ref=ref,
            actions={a for _, a, _ in events},
            authors={w for _, _, who in events for w in who},
            events=events,
            reads=list(reads),
            versions=n,
            bodies=[patient("x")] * n if bodies is None else bodies,
        )
    }


def test_a_create_the_agent_corrected_is_verified_and_says_so():
    x = patient("x")
    r = judge(cc(), PARAMS, base(), fixed(), {"x": x}, x)
    assert (r.outcome, r.record_id) == ("created_verified", "x")
    assert r.evidence["corrected"] is True and r.evidence["writes_to_record"] == 2
    assert "corrected" in r.reasons[0]


def test_corrections_can_be_turned_off():
    x = patient("x")
    r = judge(cc(accept_corrections=False), PARAMS, base(), fixed(), {"x": x}, x)
    assert r.outcome == "needs_review"


def test_a_correction_needs_settle_time_and_history():
    x = patient("x")
    assert judge(contract(), PARAMS, base(), fixed(), {"x": x}, x).outcome == (
        "needs_review"
    )
    changes = fixed()
    changes["Patient/x"].versions = None
    assert judge(cc(), PARAMS, base(), changes, {"x": x}, x).outcome == "needs_review"


def test_a_correction_that_leaves_a_wrong_value_needs_review():
    x = patient("x", birthDate="1984-07-03")
    r = judge(cc(), PARAMS, base(), fixed(), {"x": x}, x)
    assert r.outcome == "needs_review" and "corrected" not in r.evidence


def test_a_correction_that_changes_who_the_record_is_needs_review():
    # Version 1 was someone else: a search for them may have found it.
    bob = patient(
        "x",
        name=[{"given": ["Bob"], "family": "Stone"}],
        birthDate="1990-01-01",
        telecom=[],
    )
    x = patient("x")
    r = judge(cc(), PARAMS, base(), fixed(bodies=[bob, x]), {"x": x}, x)
    assert r.outcome == "needs_review"
    assert any("not the person" in reason for reason in r.reasons)


def test_another_actor_reading_between_the_writes_needs_review():
    x = patient("x")
    seen = [(_t(2), frozenset({READER}))]
    r = judge(cc(), PARAMS, base(), fixed(reads=seen), {"x": x}, x)
    assert r.outcome == "needs_review"
    assert any("read the record" in reason for reason in r.reasons)


def test_a_read_logged_just_after_the_last_fix_still_counts():
    x = patient("x")
    seen = [(_t(3.001), frozenset({READER}))]
    r = judge(cc(), PARAMS, base(), fixed(reads=seen), {"x": x}, x)
    assert r.outcome == "needs_review"


def test_the_agents_own_reads_dont_count():
    x = patient("x")
    seen = [(_t(2), frozenset({BOT})), (_t(4), frozenset({BOT}))]
    r = judge(cc(), PARAMS, base(), fixed(reads=seen), {"x": x}, x)
    assert r.outcome == "created_verified"


def test_an_anonymous_read_between_the_writes_needs_review():
    x = patient("x")
    seen = [(_t(2), frozenset())]
    r = judge(cc(), PARAMS, base(), fixed(reads=seen), {"x": x}, x)
    assert r.outcome == "needs_review"


def test_a_correction_with_a_second_create_hides_no_duplicate():
    x, y = patient("x"), patient("y")
    changes = {**fixed(), **ch("Patient/y", "C")}
    r = judge(cc(), PARAMS, base(), changes, {"x": x, "y": y}, x)
    assert r.outcome == "needs_review"


def test_a_correction_with_a_write_elsewhere_hides_no_collateral():
    x = patient("x")
    changes = {**fixed(), **ch("Patient/z", "U")}
    r = judge(cc(), PARAMS, base(), changes, {"x": x}, x)
    assert r.outcome == "needs_review"


def test_a_delete_and_recreate_is_not_a_correction():
    x = patient("x")
    changes = fixed((_t(4), "D", frozenset({BOT})))
    r = judge(cc(), PARAMS, base(), changes, {"x": x}, x)
    assert r.outcome == "needs_review"


def test_an_update_by_someone_else_is_not_a_correction():
    x = patient("x")
    changes = fixed((_t(4), "U", frozenset({OTHER})))
    r = judge(cc(), PARAMS, base(), changes, {"x": x}, x)
    assert r.outcome == "needs_review"


def test_an_update_with_no_actor_or_no_time_is_not_a_correction():
    x = patient("x")
    for extra in [(_t(4), "U", frozenset()), (None, "U", frozenset({BOT}))]:
        r = judge(cc(), PARAMS, base(), fixed(extra), {"x": x}, x)
        assert r.outcome == "needs_review", extra


def test_a_version_the_trail_did_not_log_is_not_a_correction():
    x = patient("x")
    r = judge(cc(), PARAMS, base(), fixed(versions=3), {"x": x}, x)
    assert r.outcome == "needs_review"
    assert any("history" in reason for reason in r.reasons)


def test_an_update_logged_before_the_create_is_not_a_correction():
    x = patient("x")
    events = [(_t(3), "C", frozenset({BOT})), (_t(1), "U", frozenset({BOT}))]
    changes = {
        "Patient/x": Change(
            ref="Patient/x",
            actions={"C", "U"},
            authors={BOT},
            events=events,
            versions=2,
            bodies=[x, x],
        )
    }
    r = judge(cc(), PARAMS, base(), changes, {"x": x}, x)
    assert r.outcome == "needs_review"


def test_without_an_audit_trail_a_correction_needs_review():
    x = patient("x")
    c = cc(change_feed={"audit": None, "sweep_types": ["Patient"]})
    r = judge(c, PARAMS, base(), fixed(), {"x": x}, x)
    assert r.outcome == "needs_review"


def test_a_correction_after_the_person_existed_still_needs_review():
    e, x = patient("e"), patient("x")
    r = judge(cc(), PARAMS, base(e), fixed(), {"e": e, "x": x}, x)
    assert r.outcome == "needs_review"


def _stamped(action, ref, when, who=BOT, *more):
    event = {
        **audit(action, ref, who),
        "id": f"a-{action}-{ref}-{when}",
        "recorded": when,
    }
    event["entity"] += [{"what": {"reference": r}} for r in more]
    return event


def _iso(seconds: float) -> str:
    return _t(seconds).isoformat().replace("+00:00", "Z")


def test_reader_keeps_write_times_and_reads_of_written_records():
    r = reader_for(
        {
            "AuditEvent": bundle(
                _stamped("C", "Patient/x", _iso(1)),
                _stamped("R", "Patient/x/_history/1", _iso(2), READER),
                _stamped("R", "Patient/q", _iso(2), READER, "Patient/x"),
                _stamped("U", "Patient/x", _iso(3)),
            ),
            "Observation": bundle(),
            "Patient/x/_history": bundle(
                versioned(patient("x"), BOT, _iso(1)),
                versioned(patient("x"), BOT, _iso(3)),
                kind="history",
            ),
        }
    )
    changes = read_changes(
        contract(change_feed={"audit": {}, "sweep_types": ["Observation"]}), r, T0
    )
    x = changes["Patient/x"]
    assert [a for _, a, _ in x.events] == ["C", "U"]
    # A versioned entity, and a second entity, are reads of Patient/x.
    assert x.reads == [(_t(2), frozenset({READER}))] * 2
    assert x.versions == 2 and len(x.bodies) == 2 and "Patient/q" not in changes


class _Server:
    """A FHIR server that logs every read by id, as Medplum does."""

    def __init__(self, *writes):
        self.events = [_stamped(a, "Patient/x", _iso(s)) for a, s in writes]
        self.history = [versioned(patient("x"), BOT, _iso(s)) for _, s in writes]
        self.now = 12.0  # the verifier's reads land after the window

    def get(self, url, params):
        key = url.split("/fhir/R4/", 1)[-1]
        params = dict(params or {})
        if key == "metadata":
            return 200, {"Date": "Wed, 28 Oct 2026 12:00:10 GMT"}, {}
        if key == "AuditEvent":
            bounds = params.get("date") or []
            bounds = [bounds] if isinstance(bounds, str) else bounds

            def inside(e):
                when = datetime.fromisoformat(e["recorded"].replace("Z", "+00:00"))
                for b in bounds:
                    at = datetime.fromisoformat(b[2:].replace("Z", "+00:00"))
                    if (b[:2] == "ge" and when < at) or (b[:2] == "le" and when > at):
                        return False
                ent = params.get("entity")
                act = params.get("action")
                return (not ent or e["entity"][0]["what"]["reference"] == ent) and (
                    not act or e["action"] == act
                )

            return 200, {}, bundle(*[e for e in self.events if inside(e)])
        if key == "Patient/x/_history":
            return 200, {}, bundle(*self.history, kind="history")
        if key == "Patient/x":
            self.events.append(
                _stamped("R", "Patient/x", _iso(self.now), "ClientApplication/verifier")
            )
            self.now += 0.1
            return 200, {}, patient("x")
        return 200, {}, bundle(patient("x"))


@pytest.mark.parametrize(
    "writes,corrected",
    [([("C", 2)], False), ([("C", 2), ("U", 3)], True)],
)
def test_a_server_that_logs_the_verifiers_own_reads(writes, corrected):
    from openadapt_flow.ensure_record import verify_ensure

    server = _Server(*writes)
    c = cc(change_feed={"audit": {"heartbeat": True}, "history_author": False})
    r = verify_ensure(
        c, PARAMS, base(), reader=FhirReader(c, server.get), sleep=lambda s: None
    )
    assert r.outcome == "created_verified", r.reasons
    assert bool(r.evidence.get("corrected")) is corrected


# -- void and corrections together ----------------------------------------------------


def test_a_correction_out_of_void_is_not_a_correction():
    # Version 1 asserted nothing, so it was never the person: a search may
    # have skipped it and another writer created the dose again.
    v, x = vax("x", "entered-in-error"), vax("x")
    changes = fixed(ref="Immunization/x", bodies=[v, x])
    r = judge(vax_contract(settle_s=1), VAX_PARAMS, base(), changes, {"x": x}, x)
    assert r.outcome == "needs_review"
    assert any("not the person" in reason for reason in r.reasons)


def test_a_correction_into_void_needs_review():
    x, v = vax("x"), vax("x", "entered-in-error")
    changes = fixed(ref="Immunization/x", bodies=[x, v])
    r = judge(vax_contract(settle_s=1), VAX_PARAMS, base(), changes, {}, v)
    assert r.outcome == "needs_review"
    assert any("asserts nothing" in reason for reason in r.reasons)


def test_a_correction_beside_a_void_lookalike_is_verified():
    x = vax("x")
    changes = fixed(ref="Immunization/x", bodies=[x, x])
    r = judge(vax_contract(settle_s=1), VAX_PARAMS, base(), changes, {"x": x}, x)
    assert (r.outcome, r.record_id) == ("created_verified", "x")
    assert r.evidence["corrected"] is True


def test_voiding_a_record_that_was_the_person_is_no_correction():
    # The agent marks the existing dose entered-in-error and creates its own:
    # the existing record was the person, and the agent wrote it.
    e, x = vax("e"), vax("x")
    changes = {
        **ch("Immunization/e", "U"),
        **fixed(ref="Immunization/x", bodies=[x, x]),
    }
    r = judge(vax_contract(settle_s=1), VAX_PARAMS, base(e), changes, {"x": x}, x)
    assert r.outcome == "needs_review"
