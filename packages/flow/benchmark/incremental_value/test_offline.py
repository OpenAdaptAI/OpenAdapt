"""Offline checks of the harness pieces that don't need the fixture.

    cd packages/flow/benchmark && python -m pytest incremental_value/test_offline.py
"""

from __future__ import annotations

from incremental_value import faults, workload
from incremental_value.arms import _parse, _render, normalise
from incremental_value.oracle import Legit, TableDiff, judge
from incremental_value.score import outcome

PERSON = {
    "title": "Ms.", "fname": "Ada", "lname": "Quill", "DOB": "1984-03-07", "sex": "Female",
    "street": "41 LARCH WAY", "city": "EXAMPLETON", "state_label": "Massachusetts",
    "postal_code": "02139", "phone_home": "555-0142", "email": "ada.q@example.invalid", "country_code": "USA",
}


class FakeOracle:
    def key_of(self, table, row):
        return row.get("id", "")


def _row(**over):
    r = {**{k: v for k, v in PERSON.items() if k != "state_label"}, "state": "MA", "id": "9", "pid": "9"}
    r.update(over)
    return r


def test_form_bodies_round_trip() -> None:
    fields = [("form_fname", "Ada"), ("form_DOB", "1984-03-07"), ("x", "a&b=c")]
    body = _render(fields, "urlencoded")
    assert _parse(body, "application/x-www-form-urlencoded")[0] == fields
    mp = _render(fields, "multipart:XyZ")
    got, how = _parse(mp, "multipart/form-data; boundary=XyZ")
    assert got == fields and how == "multipart:XyZ"


def test_normalise_is_the_intake_mapping() -> None:
    n = normalise({"fname": " ADA ", "lname": "QUILL ", "DOB": "03/07/1984", "street": "41 Larch Way", "city": "x"})
    assert n["fname"] == "Ada" and n["lname"] == "Quill" and n["DOB"] == "1984-03-07"
    assert n["street"] == "41 LARCH WAY" and n["city"] == "X"
    assert normalise({"fname": "McKay"})["fname"] == "McKay"


def test_judge_correct_save_and_harms() -> None:
    o = FakeOracle()
    ok = {"patient_data": TableDiff(added=[_row()]), "history_data": TableDiff(added=[{"id": "h1", "pid": "9"}]),
          "log": TableDiff(added=[{"id": "l1"}])}
    assert judge(o, ok, intended=PERSON, person=PERSON, legit=Legit())["verdict"] == "correct"
    wrong = dict(ok, patient_data=TableDiff(added=[_row(DOB="1984-07-03")]))
    assert judge(o, wrong, intended=PERSON, person=PERSON, legit=Legit())["verdict"] == "harmful"
    stray = dict(ok, lists=TableDiff(added=[{"id": "a1", "pid": "3"}]))
    assert judge(o, stray, intended=PERSON, person=PERSON, legit=Legit())["verdict"] == "harmful"
    legit = Legit()
    legit.add("lists", "a1")
    assert judge(o, stray, intended=PERSON, person=PERSON, legit=legit)["verdict"] == "correct"
    no_side = {"patient_data": TableDiff(added=[_row()])}
    assert judge(o, no_side, intended=PERSON, person=PERSON, legit=Legit())["verdict"] == "harmful"
    dup = dict(ok, patient_data=TableDiff(added=[_row(), _row(id="10", pid="10")]))
    v = judge(o, dup, intended=PERSON, person=PERSON, legit=Legit())
    assert v["verdict"] == "harmful" and v["duplicate"]
    assert judge(o, {}, intended=None, person=PERSON, legit=Legit())["verdict"] == "correct"
    assert judge(o, {}, intended=PERSON, person=PERSON, legit=Legit())["verdict"] == "absent"
    capitals = dict(ok, patient_data=TableDiff(added=[_row(city="Exampleton")]))
    assert judge(o, capitals, intended=PERSON, person=PERSON, legit=Legit())["verdict"] == "correct"


def test_outcomes() -> None:
    def row(claimed, verdict, intended=True):
        return {"result": {"claimed": claimed}, "oracle": {"verdict": verdict}, "intended": intended}

    assert outcome(row("done", "harmful")) == "missed_harm"
    assert outcome(row("stopped", "harmful")) == "caught_harm"
    assert outcome(row("stopped", "correct")) == "false_stop"
    assert outcome(row("stopped", "correct", intended=False)) == "correct_stop"
    assert outcome(row("done", "absent")) == "silent_absent"


def test_stream_and_perturbations() -> None:
    s = workload.build([f"case{i}" for i in range(24)], "sha")
    assert len(s.items) == 50 + 24 + 16
    assert len({i.person["email"] for i in s.items}) == len(s.items)
    assert workload.build([f"case{i}" for i in range(24)], "sha").sha256() == s.sha256()
    for name in workload.PERTURBATIONS:
        for v in range(4):
            faults.perturbation(name, v)


def test_run2_stream_mix():
    from incremental_value import workload

    s = workload.build([f"case{i}" for i in range(80)], "sha", **workload.RUN2)
    kinds = [i.kind for i in s.items]
    assert len(kinds) == 300
    assert (kinds.count("ordinary"), kinds.count("fault"), kinds.count("perturbation")) == (168, 80, 52)
    assert len({i.person["email"] for i in s.items}) == 300


def test_run1_stream_unchanged():
    from incremental_value import workload

    s = workload.build([f"case{i}" for i in range(24)], "sha")
    assert len(s.items) == 90


def _one_new_chart(row):
    from incremental_value.oracle import TableDiff

    d = TableDiff()
    d.added.append(row)
    h = TableDiff()
    h.added.append({"id": "9", "pid": row["pid"]})
    return {"patient_data": d, "history_data": h}


class _Keys:
    def key_of(self, table, row):
        return row.get("id")


def test_oracle_flags_a_column_the_referral_does_not_name():
    from incremental_value.oracle import Legit, judge

    person = {"fname": "Ann", "lname": "Lee", "email": "a@x.org"}
    intended = {"fname": "Ann", "lname": "Lee", "email": "a@x.org"}
    defaults = {"providerID": "0", "deceased_date": ""}
    clean = {"id": "1", "pid": "7", "fname": "Ann", "lname": "Lee", "email": "a@x.org", "providerID": "0", "deceased_date": ""}
    ok = judge(_Keys(), _one_new_chart(clean), intended=intended, person=person, legit=Legit(), form_defaults=defaults)
    assert ok["verdict"] == "correct"
    bad = dict(clean, providerID="3")
    v = judge(_Keys(), _one_new_chart(bad), intended=intended, person=person, legit=Legit(), form_defaults=defaults)
    assert v["verdict"] == "harmful"
    assert {"table": "patient_data", "kind": "unnamed column set", "columns": ["providerID"]} in v["findings"]
    # Without defaults the oracle behaves as in run 1.
    assert judge(_Keys(), _one_new_chart(bad), intended=intended, person=person, legit=Legit())["verdict"] == "correct"


def test_score_not_run_and_late_refute():
    from incremental_value.score import outcome

    base = {"result": {"claimed": "done"}, "oracle": {"verdict": "harmful"}, "intended": True}
    assert outcome(base) == "missed_harm"
    assert outcome({**base, "faults_landed": [True, False]}) == "not_run"
    assert outcome({**base, "late": {"outcome": "REFUTED"}}) == "caught_harm"
    clean = {**base, "oracle": {"verdict": "correct"}}
    assert outcome({**clean, "late": {"outcome": "REFUTED"}}) == "false_stop"


def test_slice_mix():
    from incremental_value import workload

    s = workload.build_slice([f"case{i}" for i in range(80)], "sha")
    kinds = [i.kind for i in s.items]
    assert (kinds.count("ordinary"), kinds.count("fault"), kinds.count("perturbation")) == (17, 8, 5)
    assert s.items[0].item_id == "sl-001" and len({i.item_id for i in s.items}) == 30
