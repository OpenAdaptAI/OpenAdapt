"""The stop card's categories and Fix, against stubbed verifier reads."""

from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from benchmark.stop_card import card as cards
from openadapt_flow.ensure_record import Baseline, Change, EnsureContract

BOT = "Practitioner/bot"
CONTRACT = EnsureContract.model_validate(
    {
        "resource_type": "Patient",
        "base_url": "http://fhir.test",
        "self": [BOT],
        "fields": {
            "fname": {"path": "name.given"},
            "lname": {"path": "name.family"},
            "email": {"path": "telecom[system=email].value", "compare": "casefold"},
        },
        "identity": [["fname", "lname"]],
        "search": {"fname": "given", "lname": "family"},
        "closed": True,
    }
)
PARAMS = {"fname": "Ann", "lname": "Lee", "email": "a@x.test"}
GOOD = {
    "resourceType": "Patient",
    "id": "p1",
    "name": [{"given": ["Ann"], "family": "Lee"}],
    "telecom": [{"system": "email", "value": "a@x.test"}],
}
BASE = Baseline(started=datetime(2026, 10, 29, tzinfo=timezone.utc), matches={})


class Reader:
    base_url = "http://fhir.test"

    def __init__(self, records):
        self.records = records

    def fetch(self, url, params=None):
        ref = url.removeprefix(self.base_url + "/")
        if ref not in self.records:
            raise cards.Unreadable("gone")
        return {}, copy.deepcopy(self.records[ref])

    def bundle(self, path, params=None):
        return []


def _stub(monkeypatch, changes, matches):
    monkeypatch.setattr(cards, "read_changes", lambda *a, **k: changes)
    monkeypatch.setattr(cards, "find_matches", lambda *a, **k: (matches, 0))


def _ch(ref, actions, *authors):
    return Change(ref=ref, actions=set(actions), authors=set(authors))


def test_a_right_record_is_approve(monkeypatch):
    _stub(monkeypatch, {"Patient/p1": _ch("Patient/p1", "C", BOT)}, {"p1": GOOD})
    card = cards.build(CONTRACT, PARAMS, BASE, Reader({"Patient/p1": GOOD}))
    assert card.category == "approve"


def test_a_wrong_field_on_the_agents_record_is_fix_and_fix_reads_back(monkeypatch):
    bad = copy.deepcopy(GOOD)
    bad["telecom"][0]["value"] = "b@x.test"
    bad["gender"] = "female"
    records = {"Patient/p1": bad}
    _stub(monkeypatch, {"Patient/p1": _ch("Patient/p1", "C", BOT)}, {"p1": bad})
    card = cards.build(CONTRACT, PARAMS, BASE, Reader(records))
    assert card.category == "fix"
    assert {r.kind for r in card.rows} >= {"wrong", "extra"}
    assert any('task "a@x.test", record "b@x.test"' in r.text for r in card.rows)

    def write(method, url, body):
        records[url.removeprefix("http://fhir.test/")] = body

    res = cards.act(card, CONTRACT, PARAMS, Reader(records), write)
    assert res.ok and res.task_done
    assert "gender" not in records["Patient/p1"]


def test_someone_else_on_the_record_is_open(monkeypatch):
    bad = copy.deepcopy(GOOD)
    bad["name"][0]["family"] = "Leigh"
    _stub(
        monkeypatch,
        {"Patient/p1": _ch("Patient/p1", "CU", BOT, "Practitioner/other")},
        {},
    )
    card = cards.build(CONTRACT, PARAMS, BASE, Reader({"Patient/p1": bad}))
    assert card.category == "open"
    assert any("someone else" in r.text for r in card.rows)


def test_collateral_with_a_right_record_is_undo_that_keeps_the_record(monkeypatch):
    changes = {
        "Patient/p1": _ch("Patient/p1", "C", BOT),
        "Observation/o1": _ch("Observation/o1", "C", BOT),
    }
    records = {"Patient/p1": GOOD, "Observation/o1": {"id": "o1"}}
    _stub(monkeypatch, changes, {"p1": GOOD})
    card = cards.build(CONTRACT, PARAMS, BASE, Reader(records))
    assert card.category == "undo" and card.keep == "Patient/p1"

    def write(method, url, body):
        assert method == "DELETE"
        records.pop(url.removeprefix("http://fhir.test/"))

    res = cards.act(card, CONTRACT, PARAMS, Reader(records), write)
    assert res.ok and res.task_done and "Patient/p1" in records


def test_an_unattributed_write_is_open(monkeypatch):
    changes = {"Patient/p1": Change(ref="Patient/p1", actions={"C"}, attributed=False)}
    _stub(monkeypatch, changes, {"p1": GOOD})
    card = cards.build(CONTRACT, PARAMS, BASE, Reader({"Patient/p1": GOOD}))
    assert card.category == "open"


@pytest.mark.parametrize("category", ["approve", "fix", "undo", "open"])
def test_person_seconds_follow_the_preregistered_model(category):
    card = cards.Card([cards.Row("wrong", "a b c d e f g h")], category, None, {})
    words = card.words()
    steps = 1.2 + words / 238 * 60 + 1.35 * 1 + 1.35
    want = 30 + (steps + 300 if category == "open" else steps + 1.2 + 2.0)
    got = card.person_seconds(1.0, 0.0 if category == "open" else 2.0)
    assert got == pytest.approx(want)
