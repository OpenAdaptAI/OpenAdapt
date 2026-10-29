"""The FHIR ensure-record contract generator: what it derives, and when it refuses."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from openadapt_flow.ensure_record import EnsureContract, load_contract
from openadapt_flow.generate import ensure_contract as gen
from openadapt_flow.generate.ensure_contract import Demo, Teach, generate, load_teach

FLOW = Path(__file__).resolve().parents[1]
TEACH = FLOW / "benchmark" / "contract_autogen" / "teach" / "medplum_patient_rebuilt"
HAND = FLOW / "benchmark" / "fhir_medplum" / "readpath" / "ensure_patient.yaml"


@pytest.fixture()
def teach() -> Teach:
    return load_teach(TEACH)


def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEDPLUM_FHIR_URL", "http://medplum.test/fhir/R4")
    monkeypatch.setenv("MEDPLUM_BOT_REF", "Practitioner/bot")


def _expand(contract: dict) -> EnsureContract:
    text = json.dumps(contract)
    text = text.replace("${MEDPLUM_FHIR_URL}", "http://medplum.test/fhir/R4")
    text = text.replace("${MEDPLUM_BOT_REF}", "Practitioner/bot")
    return EnsureContract.model_validate(json.loads(text))


def test_medplum_patient_matches_every_hand_written_line(teach, monkeypatch):
    """Each of MINUTES.md's 16 hand-written lines, generated."""

    _env(monkeypatch)
    out = generate(teach)
    assert out.admitted, out.refusals + out.to_confirm
    got, hand = _expand(out.contract), load_contract(HAND)

    assert set(got.fields) == set(hand.fields)
    for name, rule in hand.fields.items():
        mine = got.fields[name]
        assert (mine.path, mine.compare) == (rule.path, rule.compare), name
        # The hand map names the one state the task gave; the generated map
        # is the whole USPS table and agrees on every entry the hand one has.
        assert rule.map.items() <= mine.map.items(), name
    assert sorted(map(sorted, got.identity)) == sorted(map(sorted, hand.identity))
    assert got.search == hand.search
    assert (got.closed, got.allow) == (hand.closed, hand.allow)
    assert got.change_feed == hand.change_feed
    assert got.settle_s == hand.settle_s
    assert got.self_actors == hand.self_actors
    assert got.access_token_env == hand.access_token_env


def test_the_rendered_yaml_loads(teach, monkeypatch, tmp_path):
    _env(monkeypatch)
    out = generate(teach)
    path = tmp_path / "c.yaml"
    path.write_text(gen.render_yaml(out, generator="abc123"))
    assert load_contract(path).fields["email"].path == "telecom[system=email].value"


def test_cli_exit_codes(teach, tmp_path):
    assert gen.main([str(TEACH), "--out", str(tmp_path / "c.yaml")]) == 0
    assert (tmp_path / "c.yaml").exists()


# -- refusals --------------------------------------------------------------------


def _created(t: Teach, i: int) -> dict:
    d = t.demos[i]
    old = {r["id"] for r in d.before}
    return next(r for r in d.after if r["id"] not in old)


def test_list_elements_it_cant_tell_apart_refuse(teach):
    for i in range(len(teach.demos)):
        for c in _created(teach, i)["telecom"]:
            del c["system"]
    out = generate(teach)
    assert not out.admitted
    assert any("can't be told apart" in r for r in out.refusals)


def test_a_comparison_beyond_the_tables_refuses(teach):
    """Demonstrations typed the name in lower case and the record stores it
    capitalised: casefold would bind it, but no reviewed table says names
    compare casefolded, so the generator refuses rather than loosen."""

    for d in teach.demos:
        d.params["fname"] = d.params["fname"].lower()
    out = generate(teach)
    assert any(r.startswith("fname: no single element") for r in out.refusals)


def test_an_element_no_parameter_explains_refuses(teach):
    for i in range(len(teach.demos)):
        _created(teach, i)["birthSex"] = f"x{i}"
    out = generate(teach)
    assert any(r.startswith("birthSex: the record holds values") for r in out.refusals)


def test_a_constant_element_is_allowed_with_its_one_value(teach):
    for i in range(len(teach.demos)):
        _created(teach, i)["language"] = "en"
    out = generate(teach)
    assert out.admitted
    assert 'language="en"' in out.contract["allow"]


def test_a_parameter_in_one_demonstration_only_refuses(teach):
    for d in teach.demos[1:]:
        d.params.pop("email")
        old = {r["id"] for r in d.before}
        for r in d.after:
            if r["id"] not in old:
                r["telecom"] = r["telecom"][:1]
    out = generate(teach)
    assert any(r.startswith("email: given in only one") for r in out.refusals)


def test_a_demonstration_that_writes_two_records_refuses(teach):
    d = teach.demos[0]
    twin = copy.deepcopy(_created(teach, 0))
    twin["id"] = "twin"
    d.after.append(twin)
    out = generate(teach)
    assert any("created 2 Patient records" in r for r in out.refusals)


def test_a_demonstration_that_changes_another_record_refuses(teach):
    d = teach.demos[1]
    d.after[0]["meta"] = {"versionId": "changed"}
    out = generate(teach)
    assert any("changed 1 existing" in r for r in out.refusals)


@pytest.mark.parametrize("fact", gen.FACTS)
def test_a_missing_server_fact_refuses(teach, fact):
    teach.facts = {k: v for k, v in teach.facts.items() if k != fact}
    out = generate(teach)
    assert any(r.startswith("server facts not measured") for r in out.refusals)


def test_an_audit_trail_the_agent_can_rewrite_refuses(teach):
    teach.facts = dict(teach.facts, agent_cannot_write_audit=False)
    out = generate(teach)
    assert any("can change the audit trail" in r for r in out.refusals)


def test_an_identity_value_with_no_search_parameter_refuses(teach):
    teach.search_parameters = [
        sp for sp in teach.search_parameters if sp["code"] != "email"
    ]
    out = generate(teach)
    assert any(r.startswith("email: no search parameter") for r in out.refusals)


def test_a_search_parameter_the_server_does_not_list_is_not_used(teach):
    for rest in teach.capability["rest"]:
        for res in rest["resource"]:
            res["searchParam"] = [
                s for s in res["searchParam"] if s["name"] != "birthdate"
            ]
    out = generate(teach)
    assert any(r.startswith("DOB: no search parameter") for r in out.refusals)


def test_a_type_without_a_template_needs_a_person_to_confirm(teach):
    """AllergyIntolerance-like: no reviewed template, so the proposed rule
    waits for a person."""

    t = _allergy_teach()
    out = generate(t)
    assert not out.refusals, out.refusals
    assert out.to_confirm and not out.admitted
    assert out.contract["identity"] == [["code", "patient"]]
    assert out.contract["search"] == {"code": "code", "patient": "patient"}
    t.confirmed = {"identity"}
    assert generate(t).admitted


def test_the_self_check_catches_a_field_it_cannot_tell_changed(teach, monkeypatch):
    """If a table made a comparison too loose to see a change, admission fails."""

    monkeypatch.setattr(gen, "_perturb", lambda v, r: v)
    out = generate(teach)
    assert any("reads as right" in r for r in out.refusals)


# -- a type the Patient template doesn't cover ------------------------------------


def _allergy(i: int, patient: str, code: str, text: str) -> dict:
    return {
        "resourceType": "AllergyIntolerance",
        "id": f"a{i}",
        "meta": {"versionId": "1"},
        "clinicalStatus": {
            "coding": [
                {
                    "system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical",
                    "code": "active",
                }
            ]
        },
        "patient": {"reference": patient},
        "code": {
            "coding": [
                {"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": code}
            ],
            "text": text,
        },
    }


def _allergy_teach() -> Teach:
    base = load_teach(TEACH)
    rows = [("Patient/p1", "7980", "Penicillin G"), ("Patient/p2", "1191", "Aspirin")]
    demos = []
    for i, (p, c, t) in enumerate(rows):
        demos.append(
            Demo(
                f"d{i}",
                {"patient": p, "code": c, "substance": t},
                [],
                [_allergy(i, p, c, t)],
            )
        )
    sp = [
        ("patient", "reference", "AllergyIntolerance.patient"),
        ("code", "token", "AllergyIntolerance.code | AllergyIntolerance.reaction.substance | Condition.code"),
        ("clinical-status", "token", "AllergyIntolerance.clinicalStatus"),
    ]  # fmt: skip
    return Teach(
        resource_type="AllergyIntolerance",
        demos=demos,
        capability={
            "rest": [
                {
                    "resource": [
                        {
                            "type": "AllergyIntolerance",
                            "searchParam": [{"name": n, "type": k} for n, k, _ in sp],
                        }
                    ]
                }
            ]
        },
        search_parameters=[
            {"code": n, "type": k, "expression": e, "base": ["AllergyIntolerance"]}
            for n, k, e in sp
        ],
        facts=base.facts,
        deployment=base.deployment,
    )


def test_expression_paths():
    assert gen.expression_paths(
        "Patient.telecom.where(system='email') | Person.telecom", "Patient"
    ) == ["telecom[system=email]"]
    assert gen.expression_paths("Patient.name.given", "Patient") == ["name.given"]
    assert gen.expression_paths("Patient.link.other.resolve()", "Patient") == []
    assert gen.expression_paths("(Observation.value as Quantity)", "Observation") == []
