"""A record check's duplicate key made of several fields (OpenAdapt#1393).

OpenEMR's own duplicate-patient check matches on last name, first name and
date of birth. These tests use OpenEMR-shaped ``patient_data`` rows (made-up
people) and the real judging paths: :func:`judge_records`, the SQL verifier
on a sqlite copy of the table, the ``verify.yaml`` expansion, ``verify-run``,
the JIT key naming and the ``clinical-write`` policy check.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from openadapt_flow.qualify.campaign import apply_verify_config, record_effects
from openadapt_flow.qualify.config import QualifyConfigError, parse_verify_config
from openadapt_flow.record_check_binding import effect_parameter_roles
from openadapt_flow.runtime.effects import (
    Effect,
    EffectKind,
    EffectState,
    SqlRecordVerifier,
    ValueExpr,
    Verdict,
)
from openadapt_flow.runtime.effects._common import judge_records
from openadapt_flow.runtime.effects.frame import declared_selectors

KEY = ["lname", "fname", "DOB"]

#: A patient already in the chart before the run.
PRIOR = {
    "id": 1,
    "pid": 1,
    "fname": "Ada",
    "lname": "Quill",
    "DOB": "1984-03-07",
    "email": "ada.quill@example.test",
}


def _patient(id_: int, **fields: str) -> dict:
    return {**PRIOR, "id": id_, "pid": id_, **fields}


def _key_effect(**kwargs) -> Effect:
    data = dict(
        kind=EffectKind.RECORD_WRITTEN,
        match={"lname": ValueExpr(param="lname"), "fname": ValueExpr(param="fname")},
        composite_key={name: ValueExpr(param=name) for name in KEY},
        expected_count=1,
        timeout_s=0.05,
    )
    data.update(kwargs)
    return Effect(**data)


def _params(**over: str) -> dict[str, str]:
    return {
        "lname": "Quill",
        "fname": "Ada",
        "DOB": "1984-03-07",
        "email": "ada.quill@example.test",
        **over,
    }


def _state(records: list[dict]) -> EffectState:
    return EffectState(substrate="sql", reachable=True, records=records)


# -- judging ------------------------------------------------------------------


def test_same_name_different_birth_date_is_not_a_duplicate() -> None:
    sibling = _patient(2, DOB="1990-11-21", email="")
    effect = _key_effect(count_new_only=True).resolve(_params(DOB="1990-11-21"))
    verdict = judge_records(effect, _state([PRIOR]), [PRIOR, sibling], substrate="sql")
    assert verdict.verdict is Verdict.CONFIRMED, verdict.reason
    assert verdict.observed_count == 1


def test_same_name_and_birth_date_is_a_duplicate_counting_prior_records() -> None:
    # The run wrote one NEW chart (count_new_only sees exactly one), but a
    # chart for the same person already existed: the composite key refutes.
    second = _patient(2, email="")
    effect = _key_effect(count_new_only=True).resolve(_params(email=""))
    verdict = judge_records(effect, _state([PRIOR]), [PRIOR, second], substrate="sql")
    assert verdict.verdict is Verdict.REFUTED
    assert "2 records share the duplicate key (DOB, fname, lname)" in verdict.reason
    assert {r["id"] for r in verdict.matched_records} == {1, 2}


def test_the_duplicate_need_not_match_the_selector() -> None:
    # The new chart carries another email, so a selector on email alone sees
    # one record; the key on name and birth date still sees two.
    second = _patient(2, email="a.quill@example.test")
    effect = _key_effect(match={"email": ValueExpr(param="email")}).resolve(
        _params(email="a.quill@example.test")
    )
    verdict = judge_records(effect, _state([PRIOR]), [PRIOR, second], substrate="sql")
    assert verdict.verdict is Verdict.REFUTED
    assert "duplicate key" in verdict.reason

    # The single-field email key is unchanged: it does not see the duplicate.
    single = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"email": ValueExpr(param="email")},
        idempotency_key=ValueExpr(param="email"),
        key_field="email",
    ).resolve(_params(email="a.quill@example.test"))
    assert (
        judge_records(single, _state([PRIOR]), [PRIOR, second], substrate="sql").verdict
        is Verdict.CONFIRMED
    )


def test_a_missing_write_still_refutes_and_a_driver_date_matches() -> None:
    from datetime import date

    effect = _key_effect().resolve(_params(fname="Bo"))
    missing = judge_records(effect, _state([PRIOR]), [PRIOR], substrate="sql")
    assert missing.verdict is Verdict.REFUTED
    assert missing.observed_count == 0

    # A database driver may hand back a date object; it compares as its ISO text.
    typed = {**_patient(2, fname="Bo"), "DOB": date(1984, 3, 7)}
    found = judge_records(effect, _state([PRIOR]), [PRIOR, typed], substrate="sql")
    assert found.verdict is Verdict.CONFIRMED, found.reason


@pytest.mark.parametrize(
    "prior",
    [
        {"lname": "SMITH", "fname": "ADA"},
        {"lname": "Smith ", "fname": "Ada"},
        {"lname": "Smith", "fname": " Ada"},
        {"lname": "Smith", "fname": "Ada  Mae", "_want_fname": "Ada Mae"},
    ],
    ids=["upper-case", "trailing-space", "leading-space", "doubled-space"],
)
def test_a_duplicate_differing_only_in_case_or_spacing_refutes(prior: dict) -> None:
    want_fname = prior.pop("_want_fname", "Ada")
    existing = _patient(1, DOB="1984-03-07", email="", **prior)
    saved = _patient(2, lname="Smith", fname=want_fname, email="new@example.test")
    effect = _key_effect(count_new_only=True).resolve(
        _params(lname="Smith", fname=want_fname, email="new@example.test")
    )
    verdict = judge_records(
        effect, _state([existing]), [existing, saved], substrate="sql"
    )
    assert verdict.verdict is Verdict.REFUTED, verdict.reason
    assert "2 records share the duplicate key" in verdict.reason


def test_a_driver_datetime_birth_date_counts_as_the_same_date() -> None:
    from datetime import datetime

    existing = {**_patient(1, email=""), "DOB": datetime(1984, 3, 7, 0, 0)}
    saved = _patient(2, email="new@example.test")
    effect = _key_effect(count_new_only=True).resolve(_params(email="new@example.test"))
    verdict = judge_records(
        effect, _state([existing]), [existing, saved], substrate="sql"
    )
    assert verdict.verdict is Verdict.REFUTED, verdict.reason
    # Read back as text, the same datetime is also the same date.
    text = {**existing, "DOB": "1984-03-07 00:00:00"}
    verdict = judge_records(effect, _state([text]), [text, saved], substrate="sql")
    assert verdict.verdict is Verdict.REFUTED, verdict.reason


def test_the_saved_record_itself_is_still_compared_exactly() -> None:
    # Loose matching never makes a save's own record count: a save whose
    # stored name differs in case from the run's value is not confirmed.
    saved = _patient(2, lname="SMITH", fname="Ada", email="new@example.test")
    effect = _key_effect(count_new_only=True).resolve(
        _params(lname="Smith", email="new@example.test")
    )
    verdict = judge_records(effect, _state([]), [saved], substrate="sql")
    assert verdict.verdict is Verdict.REFUTED
    assert verdict.observed_count == 0


def test_sql_verifier_refutes_a_second_chart(tmp_path: Path) -> None:
    path = tmp_path / "openemr.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE patient_data (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "pid INTEGER, fname TEXT, lname TEXT, DOB DATE, email TEXT)"
    )
    conn.execute(
        "INSERT INTO patient_data (pid, fname, lname, DOB, email) VALUES "
        "(1, 'Ada', 'Quill', '1984-03-07', 'ada.quill@example.test')"
    )
    conn.commit()
    verifier = SqlRecordVerifier(
        lambda: sqlite3.connect(path),
        "SELECT id, pid, fname, lname, DOB, email FROM patient_data "
        "WHERE lname = :lname",
        query_params={"lname": "Quill"},
        poll_interval_s=0.01,
    )
    sibling = _key_effect(count_new_only=True).resolve(_params(DOB="1990-11-21"))
    before = verifier.capture_pre_state()
    conn.execute(
        "INSERT INTO patient_data (pid, fname, lname, DOB, email) VALUES "
        "(2, 'Ada', 'Quill', '1990-11-21', NULL)"
    )
    conn.commit()
    assert verifier.verify(sibling, before).verdict is Verdict.CONFIRMED

    duplicate = _key_effect(count_new_only=True).resolve(_params())
    before = verifier.capture_pre_state()
    conn.execute(
        "INSERT INTO patient_data (pid, fname, lname, DOB, email) VALUES "
        "(3, 'Ada', 'Quill', '1984-03-07', NULL)"
    )
    conn.commit()
    conn.close()
    verdict = verifier.verify(duplicate, before)
    assert verdict.verdict is Verdict.REFUTED
    assert "duplicate key" in verdict.reason


# -- the contract -------------------------------------------------------------

#: A single-field key effect as verify.yaml expanded it before #1393, and the
#: digests origin/main computed for it. They must never move.
SINGLE_KEY_HASH = (
    "sha256:f64985856097097954250be6523fa260daf9fc5db495671c2548ead9a388577b"
)
SINGLE_KEY_RESOLVED_HASH = (
    "sha256:587d520ae21a3ee9607d4c6de484dc0f525f7e245bbe92006b1628a4737f69ef"
)
SINGLE_KEY_JSON = (
    '{"count_new_only": false, "expected_count": 1, "field": null, '
    '"forbid_collateral_loss": true, "idempotency_key": {"literal": null, '
    '"param": "email"}, "identity_field": "id", "key_field": "email", '
    '"kind": "record_written", "match": {"email": {"literal": null, "param": '
    '"email"}, "lname": {"literal": null, "param": "lname"}}, '
    '"needs_operator_confirmation": false, "new_records": [], "probe": '
    '"exactly 1 patient record(s) written", "readback": null, "risk": '
    '"reversible", "timeout_s": 5.0, "value": null}'
)


def _single_key_effect() -> Effect:
    return Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"email": ValueExpr(param="email"), "lname": ValueExpr(param="lname")},
        idempotency_key=ValueExpr(param="email"),
        key_field="email",
        forbid_collateral_loss=True,
        probe="exactly 1 patient record(s) written",
    )


def test_single_field_key_serialization_and_digests_are_unchanged() -> None:
    effect = _single_key_effect()
    params = {"email": "ada.quill@example.test", "lname": "Quill"}
    assert json.dumps(effect.model_dump(mode="json"), sort_keys=True) == (
        SINGLE_KEY_JSON
    )
    assert effect.contract_hash() == SINGLE_KEY_HASH
    assert effect.resolve(params).contract_hash() == SINGLE_KEY_RESOLVED_HASH
    assert effect.resolved_contract_hash(params) == SINGLE_KEY_RESOLVED_HASH
    assert Effect.model_validate_json(effect.model_dump_json()) == effect


def test_composite_key_is_bound_into_the_contract() -> None:
    effect = _key_effect()
    dumped = effect.model_dump(mode="json")
    assert dumped["composite_key"]["DOB"] == {"literal": None, "param": "DOB"}
    assert Effect.model_validate(dumped) == effect
    assert effect.referenced_params() == {"lname", "fname", "DOB"}
    one = effect.resolve(_params()).contract_hash()
    other = effect.resolve(_params(DOB="1990-11-21")).contract_hash()
    assert one != other
    assert effect.resolved_contract_hash(_params()) == one
    unkeyed = _key_effect(composite_key={})
    assert unkeyed.resolve(_params()).contract_hash() != one


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            {"kind": EffectKind.FIELD_EQUALS, "field": "email", "value": "x"},
            "only to record_written",
        ),
        (
            {"idempotency_key": ValueExpr(param="email"), "key_field": "email"},
            "not both",
        ),
        ({"expected_count": 2}, "expected_count 2"),
        ({"composite_key": {" ": ValueExpr(param="lname")}}, "blank"),
    ],
)
def test_composite_key_refuses_unclear_contracts(change: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _key_effect(**change)


def test_key_fields_feed_the_frame_selector_and_parameter_roles() -> None:
    effect = _key_effect()
    assert declared_selectors(effect) == [
        {"lname": "{lname}", "fname": "{fname}", "DOB": "{DOB}"}
    ]
    roles = effect_parameter_roles(effect)
    assert ("DOB", "composite_key.DOB") in roles


def test_policy_accepts_a_composite_key() -> None:
    from openadapt_flow.ir import ActionKind, Step
    from openadapt_flow.policy import effect_has_idempotency_key

    step = Step(
        id="s1",
        intent="Create patient",
        action=ActionKind.CLICK,
        effects=[_key_effect()],
    )
    assert effect_has_idempotency_key(step)
    step.effects = [_key_effect(composite_key={})]
    assert not effect_has_idempotency_key(step)
    # A key with an empty value is a key in name only.
    step.effects = [_key_effect(composite_key={"lname": "", "fname": "Ada"})]
    assert not effect_has_idempotency_key(step)
    step.effects = [
        Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match={"email": ValueExpr(param="email")},
            idempotency_key=ValueExpr(literal="  "),
            key_field="email",
        )
    ]
    assert not effect_has_idempotency_key(step)


# -- verify.yaml ----------------------------------------------------------------


def _verify_yaml(duplicate_key, **record) -> dict:
    return {
        "schema_version": "openadapt.verify/v1",
        "app": {
            "name": "OpenEMR",
            "version": "1",
            "url": "http://127.0.0.1:9301/",
            "reset": {"command": "true"},
        },
        "verifier": {
            "kind": "rest",
            "base_url": "http://127.0.0.1:9300",
            "records_path": "/apis/default/api/patient",
            "records_key": "data",
        },
        "record": {
            "name": "patient",
            "match": ["lname", "fname"],
            "duplicate_key": duplicate_key,
            "fields": {"lname": "lname", "fname": "fname", "DOB": "DOB"},
            **record,
        },
    }


def test_verify_yaml_list_key_expands_to_a_composite_key() -> None:
    config = parse_verify_config(_verify_yaml(KEY), env={})
    written = record_effects(config)[0]
    assert written.idempotency_key is None
    assert written.composite_key == {name: ValueExpr(param=name) for name in KEY}


def test_verify_yaml_one_item_list_is_a_one_field_composite_key() -> None:
    listed = record_effects(parse_verify_config(_verify_yaml(["lname"]), env={}))
    plain = record_effects(parse_verify_config(_verify_yaml("lname"), env={}))
    assert listed[0].composite_key == {"lname": ValueExpr(param="lname")}
    assert listed[0].idempotency_key is None
    assert plain[0].idempotency_key == ValueExpr(param="lname")
    assert listed[0].contract_hash() != plain[0].contract_hash()

    # Unlike the single-field key, it counts a record that was already there.
    existing = _patient(1, lname="Quill", email="")
    saved = _patient(2, lname="Quill", fname="Bo", email="")
    params = _params(fname="Bo", email="")
    one = listed[0].model_copy(update={"count_new_only": True}).resolve(params)
    single = plain[0].model_copy(update={"count_new_only": True}).resolve(params)
    rows = [existing, saved]
    assert (
        judge_records(one, _state([existing]), rows, substrate="sql").verdict
        is Verdict.REFUTED
    )
    assert (
        judge_records(single, _state([existing]), rows, substrate="sql").verdict
        is Verdict.CONFIRMED
    )


@pytest.mark.parametrize(
    ("key", "message"),
    [
        (["lname", "email"], "duplicate_key"),
        ([], "duplicate_key"),
        (["lname", "lname"], "twice"),
    ],
)
def test_verify_yaml_refuses_a_bad_list_key(key: list, message: str) -> None:
    with pytest.raises(QualifyConfigError, match=message):
        parse_verify_config(_verify_yaml(key), env={})


def test_verify_yaml_refuses_a_list_key_with_more_than_one_record() -> None:
    with pytest.raises(QualifyConfigError, match="expected_count 2"):
        parse_verify_config(_verify_yaml(KEY, expected_count=2), env={})
    # The single-field key is unchanged.
    parse_verify_config(_verify_yaml("lname", expected_count=2), env={})


@pytest.mark.parametrize(
    ("verifier", "message"),
    [
        ({"records_path": "/apis/default/api/patient?email={email}"}, "email"),
        ({"records_path": "/apis/default/api/patient?_limit=1000"}, "_limit"),
        (
            {
                "kind": "sql",
                "sql_query": "SELECT * FROM patient_data WHERE email = :email",
            },
            "email",
        ),
        (
            {"kind": "sql", "sql_query": "SELECT * FROM patient_data LIMIT 1000"},
            "limits its rows",
        ),
    ],
)
def test_verify_yaml_refuses_a_read_that_may_miss_the_key(
    verifier: dict, message: str
) -> None:
    data = _verify_yaml(KEY)
    data["verifier"] = {**data["verifier"], **verifier}
    with pytest.raises(QualifyConfigError, match=message):
        parse_verify_config(data, env={})
    # The single-field key keeps its old rules.
    data["record"]["duplicate_key"] = "lname"
    parse_verify_config(data, env={})


@pytest.mark.parametrize(
    "verifier",
    [
        {"records_path": "/apis/default/api/patient?lname={lname}"},
        {
            "records_path": "/apis/default/api/patient?lname={lname}&_limit=100",
            "records_total_path": "meta.total",
        },
        {
            "records_path": "/apis/default/api/patient?_limit=100",
            "records_next_path": "links.next",
        },
        {
            "kind": "sql",
            "sql_query": (
                "SELECT * FROM patient_data WHERE lname = :lname AND DOB = :DOB"
            ),
        },
    ],
)
def test_verify_yaml_accepts_a_read_that_covers_the_key(verifier: dict) -> None:
    data = _verify_yaml(KEY)
    data["verifier"] = {**data["verifier"], **verifier}
    parse_verify_config(data, env={})


def test_verify_yaml_key_on_compiled_checks() -> None:
    from openadapt_flow.ir import ActionKind, Anchor, Step, Workflow

    save = Step(
        id="s2",
        intent="Create patient",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template="templates/s2.png", region=(1, 1, 9, 9), click_point=(2, 2)
        ),
        risk="irreversible",
        effects=[
            Effect(
                kind=EffectKind.RECORD_WRITTEN,
                match={name: ValueExpr(param=name) for name in KEY},
            )
        ],
    )
    workflow = Workflow(
        name="register",
        steps=[
            Step(id="s1", intent="name", action=ActionKind.TYPE, param="lname"),
            save,
        ],
        params={"lname": "Quill", "fname": "Ada", "DOB": "1984-03-07"},
    )
    data = _verify_yaml(KEY)
    data["record"] = {"duplicate_key": KEY}
    data["save_step"] = "s2"
    config = parse_verify_config(data, env={})
    bound, notes = apply_verify_config(workflow, config)
    assert bound.effects[0].composite_key == {
        name: ValueExpr(param=name) for name in KEY
    }
    assert {"change": "duplicate_key", "step": "s2"} in notes

    # A key field the compiled check does not select by is named, not bound.
    save.effects = [
        Effect(
            kind=EffectKind.RECORD_WRITTEN, match={"lname": ValueExpr(param="lname")}
        )
    ]
    with pytest.raises(QualifyConfigError, match=r"\['fname', 'DOB'\]"):
        apply_verify_config(workflow, config)


def _openemr_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE patient_data (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "pid INTEGER, fname TEXT, lname TEXT, DOB TEXT, email TEXT)"
    )
    conn.execute(
        "INSERT INTO patient_data (pid, fname, lname, DOB, email) VALUES "
        "(1, 'Ada', 'Quill', '1984-03-07', 'ada.quill@example.test')"
    )
    conn.commit()
    return conn


@pytest.mark.parametrize(
    ("dob", "outcome"),
    [("1990-11-21", "verified"), ("1984-03-07", "reconciliation_required")],
)
def test_verify_run_judges_an_agent_save_by_the_composite_key(
    tmp_path: Path, dob: str, outcome: str
) -> None:
    from openadapt_flow.verify_run import (
        capture_baseline,
        verify_run,
    )
    from openadapt_flow.verify_run import (
        parse_verify_config as parse_verify_run_config,
    )

    path = tmp_path / "openemr.db"
    conn = _openemr_db(path)
    cfg = parse_verify_run_config(
        {
            "schema_version": "openadapt.verify/v1",
            "verifier": {
                "kind": "sql",
                "sqlite_database": str(path),
                "sql_query": "SELECT * FROM patient_data",
            },
            "record": {
                "name": "patient",
                "match": ["email", "lname"],
                "duplicate_key": KEY,
                "fields": {
                    "email": "email",
                    "lname": "lname",
                    "fname": "fname",
                    "DOB": "DOB",
                },
            },
        }
    )
    params = _params(DOB=dob, email="a.quill@example.test")
    baseline = tmp_path / "baseline.json"
    capture_baseline(cfg, params, baseline)
    conn.execute(
        "INSERT INTO patient_data (pid, fname, lname, DOB, email) VALUES "
        "(2, 'Ada', 'Quill', ?, 'a.quill@example.test')",
        (dob,),
    )
    conn.commit()
    conn.close()
    result = verify_run(
        cfg,
        params=params,
        run_record={"executor_kind": "agent", "executor_id": "openadapt_actor"},
        authorization_ref="composite-key-test",
        baseline_path=baseline,
    )
    receipt = result["receipt"]
    assert receipt["outcome"] == outcome, receipt
    if outcome != "verified":
        assert receipt["checked"]["delta"] == "refuted"


def test_verify_run_refuses_a_read_that_may_miss_the_key(tmp_path: Path) -> None:
    from openadapt_flow.verify_run import VerifyRunError
    from openadapt_flow.verify_run import (
        parse_verify_config as parse_verify_run_config,
    )

    with pytest.raises(VerifyRunError, match="email"):
        parse_verify_run_config(
            {
                "schema_version": "openadapt.verify/v1",
                "verifier": {
                    "kind": "sql",
                    "sqlite_database": str(tmp_path / "openemr.db"),
                    "sql_query": "SELECT * FROM patient_data WHERE email = :email",
                },
                "record": {
                    "name": "patient",
                    "match": ["email"],
                    "duplicate_key": KEY,
                    "fields": {
                        "email": "email",
                        "lname": "lname",
                        "fname": "fname",
                        "DOB": "DOB",
                    },
                },
            }
        )
