"""The zero-config verifier benchmark's own scoring (no browser, no server).

The ground truth must call a run wrong for each planted fault's store shape,
and the kill check must apply PROTOCOL.md's thresholds as written. Synthetic
records only.
"""

from __future__ import annotations

from benchmark.zero_config_verify.apps import MOCKLOAN, MOCKMED
from benchmark.zero_config_verify.run import ground_truth, kill_check, tally

P = {"patient_id": "p2", "type": "Consult", "note": "Synthetic note"}
SEED = {
    "id": 1,
    "patient_id": "p1",
    "type": "Triage",
    "note": "seeded",
    "source": "other",
}


def _rec(rid: int, **over: str) -> dict:
    return {"id": rid, "source": "replay", **P, **over}


def test_ground_truth_on_each_store_shape() -> None:
    assert ground_truth(MOCKMED, [SEED], [SEED, _rec(2)], P)[0]
    assert not ground_truth(MOCKMED, [SEED], [SEED], P)[0]  # nothing saved
    assert not ground_truth(MOCKMED, [], [_rec(1), _rec(2)], P)[0]  # twice
    assert not ground_truth(MOCKMED, [], [_rec(1, note="")], P)[0]  # partial
    assert not ground_truth(MOCKMED, [], [_rec(1, patient_id="p1")], P)[0]
    assert not ground_truth(MOCKMED, [SEED], [_rec(2)], P)[0]  # lost update


def test_ground_truth_checks_the_disbursement_surface() -> None:
    p = {"loan_id": "L1002", "product": "Auto", "amount": "32000", "memo": "m"}
    row = {"id": 1, "source": "replay", "surface": "disbursements", **p}
    fee = {**row, "id": 2, "surface": "fees"}
    assert ground_truth(MOCKLOAN, [], [row], p)[0]
    assert not ground_truth(MOCKLOAN, [], [row, fee], p)[0]
    assert not ground_truth(MOCKLOAN, [], [{**row, "surface": "fees"}], p)[0]


def _rows(verdicts: list[tuple[bool, str]]) -> list[dict]:
    return [
        {"fault": "f", "ground_truth_correct": ok, "verdicts": {"a": {"verdict": v}}}
        for ok, v in verdicts
    ]


def test_kill_check_thresholds() -> None:
    hand = tally(_rows([(True, "CONFIRMED"), (False, "REFUTED")]), "a")
    clean = {"runnable": True, "tally": hand}
    assert kill_check(clean, hand)["ready"]
    accepts_bad = tally(_rows([(True, "CONFIRMED"), (False, "CONFIRMED")]), "a")
    assert not kill_check({"runnable": True, "tally": accepts_bad}, hand)["ready"]
    # With 0 hand-written false rejects, one generated false reject fails.
    rejects = tally(_rows([(True, "INDETERMINATE"), (False, "REFUTED")]), "a")
    assert rejects["total"]["indeterminate"] == 1
    assert not kill_check({"runnable": True, "tally": rejects}, hand)["ready"]
    assert not kill_check({"runnable": False}, hand)["ready"]
