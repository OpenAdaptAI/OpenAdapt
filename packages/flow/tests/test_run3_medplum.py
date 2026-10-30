"""Run 3's stream and scorer, offline (no Medplum, no model)."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from benchmark.fhir_medplum.run3 import score as s3
from benchmark.fhir_medplum.run3 import stream

FAKE = {
    "people": {"zed": {"given": "Zed", "family": "Q", "birthDate": "1970-01-01"}},
    "cases": [
        {"id": f"f{i}", "type": stream.TYPES[i % 3], "class": "wrong_value"}
        for i in range(80)
    ],
}


def test_the_stream_is_run_2s_mix_at_300():
    s = stream.build(FAKE)
    items = s["cases"]
    assert len(items) == 300
    assert Counter(c["source"] for c in items) == {
        "ordinary": 168,
        "blind": 80,
        "lost_response": 13,
        "concurrent_staff": 13,
        "delayed_staff": 13,
        "restart": 13,
    }
    assert [c["item"] for c in items] == [f"iv3-{k:03d}" for k in range(1, 301)]
    assert len({c["id"] for c in items}) == 300
    assert "zed" in s["people"] and "rosa" in s["people"]


def test_the_same_seed_gives_the_same_stream():
    assert stream.build(FAKE) == stream.build(FAKE)
    assert stream.build(FAKE) != stream.build(FAKE, seed=2032)


def test_blind_cases_are_kept_as_written():
    items = {c["id"]: c for c in stream.build(FAKE)["cases"]}
    assert items["blind_f7"]["class"] == "wrong_value"
    assert items["blind_f7"]["type"] == FAKE["cases"][7]["type"]


def test_every_seeded_name_is_declared():
    s = stream.build(FAKE)
    for c in s["cases"]:
        for spec in c.get("seed") or []:
            ((_, v),) = spec.items()
            if v.get("person"):
                assert v["person"] in s["people"], c["id"]
        task = c.get("task") or {}
        if task.get("patient"):
            assert task["patient"] in s["people"], c["id"]


COSTS = json.loads(
    (
        Path(__file__).resolve().parents[1] / "benchmark/incremental_value/costs_3.json"
    ).read_text()
)
COSTS["setup_min"]["B"]["contract"] = 2.0  # measured before the run; any number here


def row(item, verdict, a_done, b_outcome, card_s=None, **kw):
    r = {
        "item": item,
        "source": "blind",
        "oracle": {"verdict": verdict},
        "claim": {"status": "done" if a_done else "not_done"},
        "verifier": {"outcome": b_outcome},
        **kw,
    }
    if card_s is not None:
        r["card"] = {
            "category": "undo",
            "person_s": {"0.5": card_s / 2, "1.0": card_s, "2.0": card_s * 2},
        }
    return r


def test_a_caught_harm_costs_the_card_for_b_and_v1_for_a():
    r = row("i1", "harmful", False, "needs_review", card_s=42.3)
    assert s3.seconds(r, "B", "caught_harm", COSTS) == 42.3
    assert s3.seconds(r, "A", "caught_harm", COSTS) == 420
    assert s3.seconds(r, "B", "caught_harm", COSTS, card=False) == 420


def test_a_false_stop_is_never_cheaper_with_a_card():
    cheap = row("i1", "correct", True, "needs_review", card_s=40)
    dear = row("i2", "correct", True, "needs_review", card_s=264)
    assert s3.seconds(cheap, "B", "false_stop", COSTS) == 120
    assert s3.seconds(dear, "B", "false_stop", COSTS) == 264


def test_left_out_items_leave_both_arms():
    rows = [
        row("i1", "harmful", True, "needs_review", card_s=42),
        row("i2", "correct", True, "created_verified"),
        {"item": "i3", "source": "blind", "not_run": "describe"},
        row("i4", "harmful", True, "created_verified", fault_delivered=False),
    ]
    doc = s3.score(rows, COSTS)
    assert doc["items_scored"] == 2 and set(doc["left_out"]) == {"i3", "i4"}
    a, b = doc["primary"]["A"], doc["primary"]["B"]
    assert (a["missed_harmful_changes"], b["missed_harmful_changes"]) == (1, 0)
    assert b["caught_harmful_changes"] == 1


def test_an_unreadable_b_is_a_stop_not_an_exclusion():
    r = row("i1", "correct", True, "needs_review")
    r["verifier"]["unreadable"] = True
    doc = s3.score([r], COSTS)
    assert doc["items_scored"] == 1
    assert doc["primary"]["B"]["false_stops"] == 1


def test_setup_is_reported_with_and_without_qualification():
    rows = [row("i1", "correct", True, "created_verified")]
    doc = s3.score(rows, COSTS)
    with_q = doc["primary"]["B"]["setup_min"]
    without = doc["sensitivity"]["setup_without_read_path_qualification"]["B"]
    assert (
        with_q - without["setup_min"]
        == COSTS["setup_min"]["B"]["read_path_qualification"]
    )


def test_a_blind_default_patient_resolves_to_its_seeded_record():
    people = {
        "rosa_d": {"given": "Rosa", "family": "Delgado", "birthDate": "1979-06-14"},
        "rosa_jr": {"given": "Rosa", "family": "Delgado", "birthDate": "2004-02-11"},
    }
    case = {
        "type": "allergy",
        "seed": [
            {"patient": {"as": "jr", "person": "rosa_jr"}},
            {"patient": {"as": "mom", "person": "rosa_d"}},
        ],
    }
    stream.resolve_patient(case, people)
    assert case["task"]["patient"] == "mom"
    named = {"type": "appointment", "task": {"patient": "rosa_d"}, "seed": case["seed"]}
    stream.resolve_patient(named, people)
    assert named["task"]["patient"] == "mom"
