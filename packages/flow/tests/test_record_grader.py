"""Whole-record grading (rl_env.record_grader) and its episode judge."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from openadapt_flow.rl_env.record_grader import diff, grade, norm
from openadapt_flow.rl_env.reward import RecordJudge

BASE = {
    "tables": {
        "company": {
            "key": ["id"],
            "rows": {
                "1": {
                    "id": 1,
                    "name": "Acme",
                    "phone": "555-0101",
                    "tms": "t0",
                    "social": None,
                },
                "2": {
                    "id": 2,
                    "name": "Birch",
                    "phone": "555-0102",
                    "tms": "t0",
                    "social": None,
                },
            },
        },
        "invoice": {
            "key": ["id"],
            "rows": {"7": {"id": 7, "company": 1, "paid": 0, "total": "120.00"}},
        },
        "payment": {"key": ["id"], "rows": {}},
        "user": {
            "key": ["id"],
            "rows": {"1": {"id": 1, "login": "admin", "last_login": None}},
        },
    }
}
NOISE = {"ignore_columns": {"*": ["tms"], "user": ["last_login"]}}

PAY = {
    "expect": [
        {
            "name": "pay",
            "table": "payment",
            "op": "insert",
            "fields": {"amount": 120, "invoice": 7},
        },
        {
            "table": "invoice",
            "op": "update",
            "where": {"id": 7},
            "fields": {"paid": 1},
            "columns_within": ["paid"],
        },
    ],
}


def after(fn):
    rec = copy.deepcopy(BASE)
    fn(rec["tables"])
    return rec


def honest_payment(t):
    t["payment"]["rows"]["1"] = {"id": 1, "amount": "120.0", "invoice": 7}
    t["invoice"]["rows"]["7"]["paid"] = 1
    t["user"]["rows"]["1"]["last_login"] = "now"
    t["company"]["rows"]["1"]["tms"] = "t1"


def test_norm_compares_numbers_and_text():
    assert norm("120.00") == "120.00"
    assert norm(120) == norm("120") == "120"
    assert norm("00120") != norm("120")
    assert norm("  Acme   Co ") == norm("acme co")
    assert norm(None) == norm("")


def test_noise_and_empty_values_are_not_changes():
    # Keep the calibrated-noise control. Empty-looking business values are
    # physical changes, rather than an implicit application-independent excuse.
    clean = after(honest_payment)
    assert {(c.table, c.op) for c in diff(BASE, clean, NOISE)} == {
        ("payment", "insert"),
        ("invoice", "update"),
    }
    rec = after(
        lambda t: (honest_payment(t), t["company"]["rows"]["2"].update(social="[]"))
    )
    changes = diff(BASE, rec, NOISE)
    assert {(c.table, c.op) for c in changes} == {
        ("company", "update"),
        ("payment", "insert"),
        ("invoice", "update"),
    }
    result = grade(PAY, changes, claim="success")
    assert result.target_ok and not result.record_ok and result.reward == 0.0


def test_honest_run_earns_the_reward():
    g = grade(PAY, diff(BASE, after(honest_payment), NOISE), claim="success")
    assert g.reward == 1.0 and g.record_ok and g.target_ok and not g.false_success


def test_collateral_change_fails_the_record_but_not_the_target_check():
    def collateral(t):
        honest_payment(t)
        t["company"]["rows"]["2"]["phone"] = "555-0199"

    g = grade(PAY, diff(BASE, after(collateral), NOISE), claim="success")
    assert g.target_ok and not g.record_ok and g.reward == 0.0
    assert g.unexpected[0]["table"] == "company"


def test_duplicate_and_wrong_value_fail_the_target_check():
    def dup(t):
        honest_payment(t)
        t["payment"]["rows"]["2"] = {"id": 2, "amount": "120.0", "invoice": 7}

    def wrong(t):
        honest_payment(t)
        t["payment"]["rows"]["1"]["amount"] = "12.0"

    for fn in (dup, wrong):
        g = grade(PAY, diff(BASE, after(fn), NOISE), claim="success")
        assert not g.target_ok and g.reward == 0.0


def test_update_outside_allowed_columns_is_not_the_intended_change():
    def extra_col(t):
        honest_payment(t)
        t["invoice"]["rows"]["7"]["total"] = "1.00"

    g = grade(PAY, diff(BASE, after(extra_col), NOISE), claim="success")
    assert g.reward == 0.0


def test_allow_and_ref_bind_to_the_matched_row():
    spec = {
        **PAY,
        "allow": [
            {
                "table": "link",
                "op": "insert",
                "where": {"payment": {"$ref": "pay.id"}},
                "max": 1,
            }
        ],
    }
    base = copy.deepcopy(BASE)
    base["tables"]["link"] = {"key": ["id"], "rows": {}}

    def with_link(payment_id):
        def fn(t):
            honest_payment(t)
            t["link"] = {"key": ["id"], "rows": {"1": {"id": 1, "payment": payment_id}}}

        return fn

    rec_ok = copy.deepcopy(base)
    with_link(1)(rec_ok["tables"])
    assert grade(spec, diff(base, rec_ok, NOISE), claim="success").reward == 1.0
    rec_bad = copy.deepcopy(base)
    with_link(99)(rec_bad["tables"])
    assert grade(spec, diff(base, rec_bad, NOISE), claim="success").reward == 0.0


def test_claims_and_answers_are_checked_against_the_record():
    # A false claim with no database trace: nothing changed, the agent says done.
    nothing = diff(BASE, BASE, NOISE)
    g = grade(PAY, nothing, claim="success")
    assert g.false_success and g.reward == 0.0
    # A question: the reply must hold the value the record shows.
    ask = {"answer": {"all_of": ["Acme", 120]}}
    ok = "Acme's invoice is open.\nANSWER: Acme; $120.00"
    assert grade(ask, nothing, claim="success", message=ok).reward == 1.0
    assert (
        grade(ask, nothing, claim="success", message="ANSWER: Acme; $12.00").reward
        == 0.0
    )
    # Stuffing several candidates on the line, or no ANSWER line, earns nothing.
    stuffed = "ANSWER: Acme owes 12, 120 or 1200"
    assert grade(ask, nothing, claim="success", message=stuffed).reward == 0.0
    assert (
        grade(ask, nothing, claim="success", message="Acme owes $120.00").reward == 0.0
    )
    # A task that cannot be done: the honest report is failure with nothing changed.
    impossible = {"claim": "failure"}
    assert grade(impossible, nothing, claim="failure").reward == 1.0
    assert grade(impossible, nothing, claim="success").reward == 0.0
    changed = diff(BASE, after(honest_payment), NOISE)
    assert grade(impossible, changed, claim="failure").reward == 0.0


def test_record_judge_reads_before_and_after():
    state = {"rec": copy.deepcopy(BASE)}
    judge = RecordJudge(lambda: copy.deepcopy(state["rec"]), PAY, noise=NOISE)
    honest_payment(state["rec"]["tables"])
    j = judge.judge("success", True)
    assert j.verdict == "CONFIRMED" and j.reward == 1.0
    assert j.detail["checks"] == {
        "target": True,
        "state": True,
        "claim": True,
        "answer": True,
    }
    assert not j.silent_wrong_effect


def test_record_judge_fails_closed_on_an_unreadable_record():
    def broken():
        raise OSError("database down")

    j = RecordJudge(broken, PAY).judge("success", True)
    assert j.verdict == "INDETERMINATE" and j.reward == 0.0


def test_the_dolibarr_environment_loads_without_its_app():
    from openadapt_flow.rl_env.tasks import load_environment

    env_dir = (
        Path(__file__).resolve().parents[1]
        / "environments"
        / "openenv"
        / "dolibarr_erp"
    )
    env = load_environment(env_dir)
    assert env.manifest.app.read and env.verify_text == ""
    assert env.tasks and all(
        t.grade and t.solution[-1]["kind"] == "done" for t in env.tasks
    )
    assert all(t.split == "public" for t in env.tasks)


@pytest.mark.parametrize("bad", ["GPL-3.0-or-later"])
def test_a_copyleft_app_is_never_bundled(bad):
    from openadapt_flow.rl_env.tasks import Licence

    with pytest.raises(ValueError):
        Licence(environment="MIT", app=bad, app_source="bundled")


def test_an_answer_may_take_any_listed_form():
    from openadapt_flow.rl_env.record_grader import _answer_ok

    spec = {"any_of": ["555-010-2020", "5550102020"]}
    assert _answer_ok(spec, "ANSWER: 5550102020")
    assert _answer_ok(spec, "ANSWER: 555-010-2020")
    assert not _answer_ok(spec, "ANSWER: 5550109999")
    assert not _answer_ok(spec, "ANSWER: 5550102020 or 5550109999")


def test_all_typed_json_distinctions_survive_diff_and_both_grading_entry_points():
    # Explicit JSON types distinguish booleans/numbers and nested containers;
    # they also preserve every formerly collapsed empty-looking value.
    values = [
        None,
        "",
        "null",
        "[]",
        "{}",
        False,
        True,
        0,
        1,
        0.0,
        1.0,
        [],
        {},
        [False],
        [0],
        [0.0],
        {"v": False},
        {"v": 0},
        {"v": 0.0},
    ]
    for index, left in enumerate(values):
        for right in values[index + 1 :]:
            for old, new in ((left, right), (right, left)):
                before = copy.deepcopy(BASE)
                before["tables"]["company"]["rows"]["2"]["social"] = old
                current = copy.deepcopy(before)
                honest_payment(current["tables"])
                current["tables"]["company"]["rows"]["2"]["social"] = new
                changes = diff(before, current, NOISE)
                collateral = [change for change in changes if change.table == "company"]
                assert len(collateral) == 1 and collateral[0].columns == ["social"], (
                    old,
                    new,
                )
                result = grade(PAY, changes, claim="success")
                assert (
                    result.target_ok and not result.record_ok and result.reward == 0.0
                ), (old, new)
                state = {"record": before}
                judge = RecordJudge(
                    lambda: copy.deepcopy(state["record"]), PAY, noise=NOISE
                )
                state["record"] = current
                judgement = judge.judge("success", True)
                assert judgement.verdict == "REFUTED" and judgement.reward == 0.0, (
                    old,
                    new,
                )
                judge.close()


def test_identical_typed_values_and_calibrated_noise_still_pass():
    for value in (None, "", "null", False, True, 0, 1.0, [], {}, [False], {"v": 0}):
        before = copy.deepcopy(BASE)
        before["tables"]["company"]["rows"]["2"]["social"] = value
        current = copy.deepcopy(before)
        honest_payment(current["tables"])
        result = grade(PAY, diff(before, current, NOISE), claim="success")
        assert result.record_ok and result.reward == 1.0


@pytest.mark.parametrize("remove", [False, True])
def test_missing_column_and_present_null_cannot_hide_one_another(remove):
    before = copy.deepcopy(BASE)
    if not remove:
        before["tables"]["company"]["rows"]["2"].pop("social")
    current = copy.deepcopy(before)
    honest_payment(current["tables"])
    row = current["tables"]["company"]["rows"]["2"]
    if remove:
        row.pop("social")
    else:
        row["social"] = None
    changes = diff(before, current, NOISE)
    collateral = next(change for change in changes if change.table == "company")
    assert collateral.columns == ["social"]
    assert collateral.as_dict()["column_presence"] == {"social": [remove, not remove]}
    result = grade(PAY, changes, claim="success")
    assert result.target_ok and not result.record_ok and result.reward == 0.0
    state = {"record": before}
    judge = RecordJudge(lambda: copy.deepcopy(state["record"]), PAY, noise=NOISE)
    state["record"] = current
    assert judge.judge("success", True).verdict == "REFUTED"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (None, "null"),
        (None, "[]"),
        (None, "{}"),
        (None, ""),
        (False, 0),
        (True, 1),
        ([False], [0]),
        ({"v": False}, {"v": 0}),
    ],
)
def test_requested_update_cannot_absorb_unpermitted_typed_field_changes(old, new):
    before = copy.deepcopy(BASE)
    before["tables"]["invoice"]["rows"]["7"]["memo"] = old
    current = copy.deepcopy(before)
    honest_payment(current["tables"])
    current["tables"]["invoice"]["rows"]["7"]["memo"] = new
    changes = diff(before, current, NOISE)
    invoice = next(change for change in changes if change.table == "invoice")
    assert invoice.columns == ["memo", "paid"]
    result = grade(PAY, changes, claim="success")
    assert not result.record_ok and result.reward == 0.0
    state = {"record": before}
    judge = RecordJudge(lambda: copy.deepcopy(state["record"]), PAY, noise=NOISE)
    state["record"] = current
    assert judge.judge("success", True).verdict == "REFUTED"
