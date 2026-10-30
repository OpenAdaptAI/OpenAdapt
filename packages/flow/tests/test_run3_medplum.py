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
COSTS["setup_min"]["B"]["contract"] = COSTS["setup_min"]["C"]["contract"] = 2.0


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


def test_c_charges_a_late_catch_between_now_and_the_cap():
    r = row("i1", "harmful", True, "needs_review", card_s=42.3)
    assert s3.seconds(r, "C", "caught_harm", COSTS) == 42.3 + 120
    dear = row("i2", "harmful", True, "needs_review", card_s=339.1)
    assert s3.seconds(dear, "C", "caught_harm", COSTS) == 339.1  # never below now
    capped = row("i3", "harmful", True, "needs_review", card_s=250)
    assert s3.seconds(capped, "C", "caught_harm", COSTS) == 330


def test_c_costs_like_b_when_the_agent_itself_stopped():
    r = row("i1", "harmful", False, "needs_review", card_s=42.3)
    assert s3.seconds(r, "C", "caught_harm", COSTS) == 42.3
    f = row("i2", "correct", True, "needs_review", card_s=40)
    assert (
        s3.seconds(f, "C", "false_stop", COSTS) == 120 + 30
    )  # right state: interrupt only


def test_a_late_catch_can_be_read_as_used_downstream():
    r = row(
        "i1", "harmful", True, "needs_review", card_s=42.3, detect_from_claim_s=75.0
    )
    assert s3.arm_outcome(r, "C") == "caught_harm"
    assert s3.arm_outcome(r, "C", downstream_after=60) == "missed_harm"
    assert s3.arm_outcome(r, "C", downstream_after=300) == "caught_harm"
    assert s3.arm_outcome(r, "B", downstream_after=60) == "caught_harm"


def test_three_comparisons_are_scored():
    doc = s3.score([row("i1", "harmful", True, "needs_review", card_s=42)], COSTS)
    assert {"B vs A", "C vs A", "C vs B"} <= set(doc["primary"]["verdicts"])
    assert doc["primary"]["verdicts"]["C vs A"]["1_fewer_missed_harms"] is True


def test_a_stop_decided_before_the_agent_ran_is_not_late():
    r = row(
        "i1", "harmful", True, "needs_review", card_s=42.3, verifier_before_agent=True
    )
    assert not s3.late(r)
    assert s3.seconds(r, "C", "caught_harm", COSTS) == 42.3


def test_a_late_catch_with_no_clock_counts_as_used_first():
    r = row("i1", "harmful", True, "needs_review", card_s=42.3)
    assert s3.arm_outcome(r, "C", downstream_after=900) == "missed_harm"


def test_c_waits_only_for_the_claims_it_holds():
    rows = [
        row("i1", "correct", True, "created_verified", agent_s=30, verify_s=5),
        row("i2", "correct", False, "created_verified", agent_s=30, verify_s=5),
    ]
    s = {a: s3.arm_summary(rows, a, COSTS) for a in ("A", "B", "C")}
    assert sorted([s["A"]["wall_s_median"], s["B"]["wall_s_median"]]) == [30, 35]
    c_walls = sorted(
        30 + (5 if not r["claim"]["status"] == "done" else 0) for r in rows
    )
    assert s["C"]["wall_s_median"] == c_walls[1]


def test_late_stops_are_counted_without_a_clock():
    rows = [row("i1", "harmful", True, "needs_review", card_s=42.3)]
    assert s3.arm_summary(rows, "C", COSTS)["late_stops"] == 1


def test_the_c_no_worse_reading_can_hold():
    # One late catch (+120 s) against a 300 s check on each of 3 items.
    rows = [
        row("i1", "harmful", True, "needs_review", card_s=42.3, verify_s=300),
        row("i2", "correct", True, "created_verified", verify_s=300),
        row("i3", "correct", True, "created_verified", verify_s=300),
    ]
    v = s3.score(rows, COSTS)["primary"]["verdicts"]["C no worse than B (secondary)"]
    assert v["extra_people_s_per_item"] == 40.0 and v["holds"] is True
    assert s3.score(rows, COSTS)["primary"]["verdicts"]["C vs B"]["wins"] is False
