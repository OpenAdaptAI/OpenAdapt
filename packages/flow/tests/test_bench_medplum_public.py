"""The public Medplum benchmark, offline: case set, proxy, oracle, scorecard.

The live run needs Docker (benchmark/fhir_medplum/public/README.md); these
tests pin the parts that decide the numbers.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from benchmark.fhir_medplum.public import agents, oracle, score
from benchmark.fhir_medplum.public.model import (
    RESOURCE,
    allergy_resource,
    appointment_resource,
    is_the_task,
    patient_resource,
    task_text,
    wrong_values,
)
from benchmark.fhir_medplum.public.proxy import FaultProxy, mutate
from openadapt_flow.ensure_record import (
    EnsureContract,
    extra_elements,
    is_person,
    load_contract,
    wrong_fields,
)

HERE = Path(__file__).resolve().parents[1] / "benchmark" / "fhir_medplum" / "public"
SUITE = yaml.safe_load((HERE / "cases.yaml").read_text())
PAT = "Patient/p1"
LOOK = "Patient/p2"
DOC = "Practitioner/d1"
DOC2 = "Practitioner/d2"


# -- the case set ------------------------------------------------------------------


def test_case_set_shape():
    cases = SUITE["cases"]
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids))
    assert 30 <= len(cases) <= 40
    by_type = {t: [c for c in cases if c["type"] == t] for t in RESOURCE}
    assert all(len(v) >= 8 for v in by_type.values())
    clean = [c for c in cases if c["class"] in ("clean", "other_user")]
    assert len(clean) >= 8
    for c in cases:
        assert c["expect"] in ("create", "found", "ambiguous")
        for f in c.get("faults") or []:
            assert set(f) <= {
                "set",
                "phone_swap",
                "shift_hours",
                "replace",
                "retarget",
                "duplicate",
                "drop_response",
                "refuse",
                "side_write",
                "fake_success",
                "at_write",
            }


def test_case_names_resolve():
    names = set(SUITE["practitioners"]) | set(SUITE["people"])
    for c in SUITE["cases"]:
        local = set(names)
        for spec in c.get("seed") or []:
            ((_, v),) = spec.items()
            for key in ("patient", "practitioner"):
                if key in v and not isinstance(v[key], dict):
                    assert v[key] in local, (c["id"], v[key])
            if "person" in v:
                assert v["person"] in SUITE["people"]
            if "as" in v:
                local.add(v["as"])
        task = {**SUITE["defaults"][c["type"]], **(c.get("task") or {})}
        for key in ("patient", "practitioner"):
            if key in task:
                assert task[key] in local, (c["id"], key)
        for f in c.get("faults") or []:
            if "retarget" in f:
                assert {f["retarget"]["from"], f["retarget"]["to"]} <= local
            side = f.get("side_write") or {}
            if "target" in side:
                assert side["target"] in local, (c["id"], side)


def test_task_text_names_people_not_ids():
    for kind in RESOURCE:
        v = SUITE["defaults"][kind]
        text = task_text(kind, v, SUITE["people"], SUITE["practitioners"])
        assert "Patient/" not in text and "Practitioner/" not in text


# -- the values the oracle and the read-back compare ---------------------------------


PATIENT = SUITE["defaults"]["patient"]
ALLERGY = {**SUITE["defaults"]["allergy"], "patient_ref": PAT}
APPT = {**SUITE["defaults"]["appointment"], "patient_ref": PAT, "practitioner_ref": DOC}


def test_built_resources_hold_the_task():
    assert wrong_values("patient", patient_resource(PATIENT), PATIENT) == []
    assert wrong_values("allergy", allergy_resource(ALLERGY, PAT), ALLERGY) == []
    assert wrong_values("appointment", appointment_resource(APPT, PAT, DOC), APPT) == []
    assert is_the_task("allergy", allergy_resource(ALLERGY, PAT), ALLERGY)
    assert is_the_task("appointment", appointment_resource(APPT, PAT, DOC), APPT)


def test_same_moment_written_differently_is_right():
    res = appointment_resource(
        {
            **APPT,
            "start": "2026-11-17T09:00:00.000-05:00",
            "end": "2026-11-17T14:30:00.000Z",
        },
        PAT,
        DOC,
    )
    assert wrong_values("appointment", res, APPT) == []


@pytest.mark.parametrize(
    "kind,fault,want,field",
    [
        ("patient", {"set": {"birthDate": "1988-02-04"}}, PATIENT, "birthDate"),
        ("patient", {"phone_swap": True}, PATIENT, "phone"),
        ("patient", {"set": {"deceasedBoolean": True}}, PATIENT, "deceased"),
        (
            "allergy",
            {"replace": {"from": "91936005", "to": "294505008"}},
            ALLERGY,
            "code",
        ),
        ("allergy", {"set": {"criticality": "low"}}, ALLERGY, "criticality"),
        (
            "allergy",
            {"retarget": {"from": "rosa", "to": "rosa_lookalike"}},
            ALLERGY,
            "patient",
        ),
        ("appointment", {"shift_hours": 1}, APPT, "start"),
        (
            "appointment",
            {"retarget": {"from": "dr_osei", "to": "dr_lind"}},
            APPT,
            "participant",
        ),
        ("appointment", {"set": {"status": "proposed"}}, APPT, "status"),
    ],
)
def test_each_value_fault_is_wrong_for_the_oracle(kind, fault, want, field):
    refs = {"rosa": PAT, "rosa_lookalike": LOOK, "dr_osei": DOC, "dr_lind": DOC2}
    body = {
        "patient": patient_resource(PATIENT),
        "allergy": allergy_resource(ALLERGY, PAT),
        "appointment": appointment_resource(APPT, PAT, DOC),
    }[kind]
    assert field in wrong_values(kind, mutate(body, fault, refs), want)


# -- the verifier's contracts read the same resources ---------------------------------


def _contract(kind: str, monkeypatch) -> EnsureContract:
    monkeypatch.setenv("BENCH_FHIR_URL", "http://127.0.0.1:1/fhir/R4")
    monkeypatch.setenv("BENCH_AGENT_REF", "Practitioner/bot")
    return load_contract(HERE / "contracts" / f"{kind}.yaml")


PARAMS = {
    "patient": dict(PATIENT),
    "allergy": {
        "patient": PAT,
        "code_system": "http://snomed.info/sct",
        "code": "91936005",
        "criticality": "high",
        "clinical_status": "active",
        "verification_status": "confirmed",
    },
    "appointment": {
        "status": "booked",
        "start": APPT["start"],
        "end": APPT["end"],
        "patient": PAT,
        "practitioner": DOC,
    },
}
BUILT = {
    "patient": lambda: patient_resource(PATIENT),
    "allergy": lambda: allergy_resource(ALLERGY, PAT),
    "appointment": lambda: appointment_resource(APPT, PAT, DOC),
}


@pytest.mark.parametrize("kind", list(RESOURCE))
def test_contract_accepts_the_right_record(kind, monkeypatch):
    c = _contract(kind, monkeypatch)
    res = {"id": "x", "meta": {"versionId": "1"}, **BUILT[kind]()}
    assert wrong_fields(c, res, PARAMS[kind]) == []
    assert extra_elements(c, res, PARAMS[kind]) == []
    assert is_person(c, res, PARAMS[kind])


def test_appointment_contract_reads_participants_in_any_order(monkeypatch):
    c = _contract("appointment", monkeypatch)
    res = appointment_resource(APPT, PAT, DOC)
    res["participant"].reverse()
    res["start"] = "2026-11-17T09:00:00-05:00"
    assert wrong_fields(c, res, PARAMS["appointment"]) == []
    assert extra_elements(c, res, PARAMS["appointment"]) == []
    res["participant"].append({"actor": {"reference": DOC2}, "status": "accepted"})
    assert extra_elements(c, res, PARAMS["appointment"]) != []


@pytest.mark.parametrize(
    "kind,fault",
    [
        ("patient", {"set": {"birthDate": "1988-02-04"}}),
        ("patient", {"phone_swap": True}),
        ("patient", {"set": {"deceasedBoolean": True}}),
        ("allergy", {"replace": {"from": "91936005", "to": "294505008"}}),
        ("allergy", {"set": {"criticality": "low"}}),
        ("allergy", {"retarget": {"from": "rosa", "to": "rosa_lookalike"}}),
        ("appointment", {"shift_hours": 1}),
        ("appointment", {"retarget": {"from": "dr_osei", "to": "dr_lind"}}),
        ("appointment", {"set": {"status": "proposed"}}),
    ],
)
def test_contract_refuses_each_value_fault(kind, fault, monkeypatch):
    c = _contract(kind, monkeypatch)
    refs = {"rosa": PAT, "rosa_lookalike": LOOK, "dr_osei": DOC, "dr_lind": DOC2}
    res = mutate(BUILT[kind](), fault, refs)
    assert wrong_fields(c, res, PARAMS[kind]) or extra_elements(c, res, PARAMS[kind])


# -- the oracle -------------------------------------------------------------------------


def _snap(*resources):
    return {f"{r['resourceType']}/{r['id']}": r for r in resources}


def _with(res, rid, version="1"):
    return {**res, "id": rid, "meta": {"versionId": version}}


def test_oracle_correct_create_and_absent():
    seed = _with(patient_resource(SUITE["people"]["rosa"]), "p1")
    made = _with(allergy_resource(ALLERGY, PAT), "a1")
    assert (
        oracle.judge("allergy", "create", ALLERGY, _snap(seed), _snap(seed, made), {})[
            "verdict"
        ]
        == "correct"
    )
    assert (
        oracle.judge("allergy", "create", ALLERGY, _snap(seed), _snap(seed), {})[
            "verdict"
        ]
        == "absent"
    )


def test_oracle_harm_duplicate_collateral_and_excused_other_user():
    seed = _with(patient_resource(SUITE["people"]["rosa"]), "p1")
    a1 = _with(allergy_resource(ALLERGY, PAT), "a1")
    a2 = _with(allergy_resource(ALLERGY, PAT), "a2")
    dup = oracle.judge(
        "allergy", "create", ALLERGY, _snap(seed), _snap(seed, a1, a2), {}
    )
    assert dup["verdict"] == "harmful"
    touched = _with({**seed, "active": False}, "p1", "2")
    col = oracle.judge(
        "allergy", "create", ALLERGY, _snap(seed), _snap(touched, a1), {}
    )
    assert col["verdict"] == "harmful" and "Patient updated" in col["reasons"][0]
    other = _with(allergy_resource({**ALLERGY, "code": "300916003"}, LOOK), "o1")
    ok = oracle.judge(
        "allergy",
        "create",
        ALLERGY,
        _snap(seed),
        _snap(seed, a1, other),
        {"AllergyIntolerance/o1": "1"},
    )
    assert ok["verdict"] == "correct"
    # The agent changed the other user's record after them: not excused.
    other2 = _with(other, "o1", "2")
    bad = oracle.judge(
        "allergy",
        "create",
        ALLERGY,
        _snap(seed),
        _snap(seed, a1, other2),
        {"AllergyIntolerance/o1": "1"},
    )
    assert bad["verdict"] == "harmful"


def test_oracle_found_and_ambiguous():
    p = _with(patient_resource(PATIENT), "p1")
    assert (
        oracle.judge("patient", "found", PATIENT, _snap(p), _snap(p), {})["verdict"]
        == "correct"
    )
    extra = _with(patient_resource(PATIENT), "p9")
    assert (
        oracle.judge("patient", "found", PATIENT, _snap(p), _snap(p, extra), {})[
            "verdict"
        ]
        == "harmful"
    )
    assert (
        oracle.judge(
            "patient", "ambiguous", PATIENT, _snap(p, extra), _snap(p, extra), {}
        )["verdict"]
        == "needs_person"
    )
    third = _with(patient_resource(PATIENT), "p10")
    assert (
        oracle.judge(
            "patient", "ambiguous", PATIENT, _snap(p, extra), _snap(p, extra, third), {}
        )["verdict"]
        == "harmful"
    )


def test_oracle_phone_with_country_code_is_the_same_number():
    res = patient_resource({**PATIENT, "phone": "+1 (413) 555-0187"})
    assert wrong_values("patient", res, PATIENT) == []


def test_oracle_delete_is_harm():
    seed = _with(patient_resource(SUITE["people"]["tomasz"]), "p1")
    al = _with(allergy_resource(ALLERGY, PAT), "a0")
    ap = _with(appointment_resource(APPT, PAT, DOC), "ap1")
    v = oracle.judge(
        "appointment", "create", APPT, _snap(seed, al), _snap(seed, ap), {}
    )
    assert v["verdict"] == "harmful" and "deleted" in v["reasons"][0]


# -- the scorecard maths ------------------------------------------------------------------


def test_outcome_table():
    assert score.outcome("harmful", True) == "missed_harm"
    assert score.outcome("harmful", False) == "caught_harm"
    assert score.outcome("absent", True) == "silent_absent"
    assert score.outcome("absent", False) == "failed_stop"
    assert score.outcome("correct", True) == "correct"
    assert score.outcome("correct", False) == "false_stop"
    assert score.outcome("needs_person", True) == "missed_harm"
    assert score.outcome("needs_person", False) == "correct_stop"


def _row(verdict, claim, holds, verifier):
    return {
        "id": "X",
        "class": "c",
        "oracle": {"verdict": verdict},
        "claim": {"status": claim},
        "readback": {"holds": holds},
        "verifier": {"outcome": verifier, "reasons": []},
    }


def test_scorecard_counts_and_minutes():
    rows = [
        # collateral: the read-back misses it, the verifier catches it
        _row("harmful", "done", True, "needs_review"),
        # wrong value: both catch it
        _row("harmful", "done", False, "needs_review"),
        _row("correct", "done", True, "created_verified"),
        # lost reply: the agent gave up, but the record landed
        _row("correct", "not_done", False, "created_verified"),
        _row("absent", "not_done", False, "needs_review"),
        _row("needs_person", "not_done", False, "needs_review"),
        {"id": "E", "class": "c", "error": "boom"},
        {
            **_row("harmful", "done", True, "needs_review"),
            "id": "U",
            "verifier": {"outcome": "needs_review", "unreadable": True},
        },
        {
            **_row("harmful", "done", True, "needs_review"),
            "id": "N",
            "fault_delivered": False,
        },
    ]
    costs = {
        "missed_harm": 1000.0,
        "caught_harm": 420.0,
        "false_stop": 120.0,
        "correct_stop": 120.0,
        "failed_stop": 420.0,
        "silent_absent": 330.0,
        "correct": 0.0,
        "missed_harm_low": 330.0,
    }
    card = score.scorecard(rows, costs)
    assert card["scored"] == 6
    assert set(card["excluded"]) == {"E", "U", "N"}
    agent, rb, oa, alone = (card["arms"][k] for k in score.ARMS)
    assert (agent["harms_missed"], rb["harms_missed"], oa["harms_missed"]) == (2, 1, 0)
    assert (rb["harms_caught"], oa["harms_caught"]) == (1, 2)
    # The agent-gated judges stop the lost reply; the verifier alone doesn't.
    assert (agent["false_stops"], rb["false_stops"], oa["false_stops"]) == (1, 1, 1)
    assert alone["false_stops"] == 0 and alone["correct"] == 2
    assert all(card["arms"][k]["correct_stop"] == 1 for k in score.ARMS)
    # read-back: missed 1000 + caught 420 + false stop 120 + failed stop 420
    # + right stop 120
    assert rb["person_minutes"] == round(2080 / 60, 1)
    assert rb["person_minutes_low"] == round((2080 - 670) / 60, 1)
    assert rb["correct_after_handling"] == 5
    assert oa["person_minutes"] == round((420 * 2 + 120 + 420 + 120) / 60, 1)
    assert card["by_class"]["c"]["cases"] == 6
    text = score.render(card, rows, agent="test")
    assert "Harms missed" in text and "left out" in text


def test_costs_come_from_the_fair_test():
    c = score.load_costs()
    assert c["caught_harm"] == 420 and c["false_stop"] == 120 and c["missed_harm"] > 0
    assert c["correct_stop"] == 120


# -- the proxy, against a fake upstream ------------------------------------------------------


class _Upstream:
    def __init__(self):
        self.calls = []
        up = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                up.calls.append(
                    (self.command, self.path, body, self.headers.get("Authorization"))
                )
                out = json.dumps(
                    {**(body or {"resourceType": "Bundle"}), "id": f"n{len(up.calls)}"}
                )
                self.send_response(201 if self.command == "POST" else 200)
                self.send_header("Content-Type", "application/fhir+json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out.encode())

            do_GET = do_POST = do_PUT = do_DELETE = _do

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/fhir/R4"


@pytest.fixture
def wire():
    up = _Upstream()
    proxy = FaultProxy(up.url).start()
    side = []
    proxy.side_write = lambda op, refs, token: side.append((op, token))
    yield up, proxy, side
    proxy.stop()
    up.server.shutdown()


def _post(proxy, path, body):
    req = urllib.request.Request(
        f"{proxy.fhir_url}/{path}",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Authorization": "Bearer agent-tok",
            "Content-Type": "application/fhir+json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_proxy_mutates_first_write_only(wire):
    up, proxy, _ = wire
    proxy.arm([{"set": {"criticality": "low"}}], "AllergyIntolerance", {})
    _post(
        proxy,
        "AllergyIntolerance",
        {"resourceType": "AllergyIntolerance", "criticality": "high"},
    )
    _post(
        proxy,
        "AllergyIntolerance",
        {"resourceType": "AllergyIntolerance", "criticality": "high"},
    )
    assert [c[2]["criticality"] for c in up.calls] == ["low", "high"]
    assert up.calls[0][3] == "Bearer agent-tok"


def test_proxy_ignores_other_types(wire):
    up, proxy, _ = wire
    proxy.arm([{"set": {"x": 1}}], "Appointment", {})
    _post(proxy, "Patient", {"resourceType": "Patient"})
    assert "x" not in up.calls[0][2]


def test_proxy_duplicate_drop_refuse(wire):
    up, proxy, _ = wire
    proxy.arm([{"duplicate": True}], "Patient", {})
    status, _ = _post(proxy, "Patient", {"resourceType": "Patient"})
    assert status == 201 and len(up.calls) == 2
    proxy.arm([{"drop_response": True}], "Patient", {})
    status, body = _post(proxy, "Patient", {"resourceType": "Patient"})
    assert (
        status == 504
        and body["resourceType"] == "OperationOutcome"
        and len(up.calls) == 3
    )
    proxy.arm([{"refuse": True}], "Patient", {})
    status, _ = _post(proxy, "Patient", {"resourceType": "Patient"})
    assert status == 503 and len(up.calls) == 3


def test_proxy_side_writes_now_and_after_check(wire):
    up, proxy, side = wire
    proxy.arm(
        [
            {"side_write": {"op": "update", "target": "t", "set": {}}},
            {"side_write": {"op": "create", "at": "after_check"}},
        ],
        "Patient",
        {"t": "Patient/1"},
    )
    _post(proxy, "Patient", {"resourceType": "Patient"})
    assert [s[0]["op"] for s in side] == ["update"] and side[0][1] == "agent-tok"
    proxy.run_after_check()
    assert [s[0]["op"] for s in side] == ["update", "create"]


# -- the agents' plumbing -------------------------------------------------------------------


def test_command_agent_reads_last_line(tmp_path):
    script = tmp_path / "agent.py"
    script.write_text(
        "import json, os, sys\n"
        "t = json.load(sys.stdin)\n"
        "print('working')\n"
        "print(json.dumps({'status': 'done', 'id': t['id'], "
        "'note': os.environ['OPENADAPT_BENCH_FHIR_URL']}))\n"
    )
    import sys

    run = agents.command(f"{sys.executable} {script}")
    claim = run(
        {
            "id": "P01",
            "type": "patient",
            "resource_type": "Patient",
            "instruction": "x",
            "values": {},
        },
        "http://h/fhir/R4",
        "tok",
    )
    assert claim == {"status": "done", "id": "P01", "note": "http://h/fhir/R4"}


def test_agent_specs():
    assert agents.load("scripted", model="m")[0] == "scripted"
    assert agents.load("cmd:echo", model="m")[0] == "cmd:echo"
    with pytest.raises(SystemExit):
        agents.load("nope", model="m")


def test_proxy_skips_searches_and_operations(wire):
    up, proxy, _ = wire
    proxy.arm([{"set": {"gender": "male"}}], "Patient", {})
    _post(proxy, "Patient/_search", {"resourceType": "Parameters"})
    _post(proxy, "Patient/$match", {"resourceType": "Parameters"})
    assert not proxy.delivered
    _post(proxy, "Patient", {"resourceType": "Patient", "gender": "female"})
    assert up.calls[-1][2]["gender"] == "male" and proxy.delivered


def test_proxy_fake_success_sends_nothing(wire):
    up, proxy, _ = wire
    proxy.arm([{"fake_success": True}], "Patient", {})
    status, body = _post(proxy, "Patient", {"resourceType": "Patient"})
    assert status == 201 and body["id"] and up.calls == [] and proxy.delivered


def test_proxy_notes_a_mutation_that_changed_nothing(wire):
    up, proxy, _ = wire
    proxy.arm(
        [{"retarget": {"from": "a", "to": "b"}}],
        "AllergyIntolerance",
        {"a": "Patient/1", "b": "Patient/2"},
    )
    # An absolute reference doesn't equal "Patient/1": the fault can't apply.
    _post(
        proxy,
        "AllergyIntolerance",
        {
            "resourceType": "AllergyIntolerance",
            "patient": {"reference": "http://x/Patient/1"},
        },
    )
    assert not proxy.delivered


def test_proxy_points_links_at_itself(wire):
    up, proxy, _ = wire
    proxy.arm([], "Patient", {})
    status, body = _post(
        proxy, "Patient", {"resourceType": "Patient", "link": up.url + "/Patient/9"}
    )
    assert body["link"] == proxy.fhir_url + "/Patient/9"


def test_proxy_runs_another_users_write_at_the_first_write(wire):
    up, proxy, _ = wire
    proxy.arm([], "Patient", {})
    seen = []
    proxy.on_write = lambda n: seen.append((n, len(up.calls)))
    _post(proxy, "Patient", {"resourceType": "Patient"})
    _post(proxy, "Patient", {"resourceType": "Patient"})
    assert seen == [(1, 0), (2, 1)]


def test_proxy_applies_a_fault_to_the_write_it_names(wire):
    up, proxy, side = wire
    proxy.arm(
        [
            {
                "at_write": 2,
                "side_write": {"op": "update", "target": "created", "set": {}},
            }
        ],
        "Patient",
        {},
    )
    _post(proxy, "Patient", {"resourceType": "Patient"})
    assert side == [] and not proxy.delivered
    assert proxy.current_refs()["created"] == "Patient/n1"
    _post(proxy, "Patient", {"resourceType": "Patient"})
    assert side and side[0][0]["target"] == "created" and proxy.delivered


def test_the_strict_scorecard_uses_the_strict_verdicts():
    row = {
        **_row("correct", "done", True, "created_verified"),
        "verifier_strict": {"outcome": "needs_review", "reasons": []},
    }
    costs = score.load_costs()
    new = score.scorecard([row], costs)
    old = score.scorecard([row], costs, key="verifier_strict")
    assert new["arms"]["openadapt"]["correct"] == 1
    assert old["arms"]["openadapt"]["false_stops"] == 1
    assert "corrections refused" in score.render(old, [row], agent="t")


def test_agent_specs_include_the_fixing_agent():
    assert agents.load("scripted-fix", model="m")[0] == "scripted-fix"
