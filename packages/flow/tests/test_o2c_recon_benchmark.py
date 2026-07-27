"""CI guard for the O2C reconciliation multi-system benchmark.

Runs the REAL harness once (n=1; deterministic, localhost only, zero model
calls) and pins the qualitative claims the committed
``benchmark/o2c_recon/results.json`` publishes:

- the healthy path spans TWO separate fixture applications plus two
  spreadsheet surfaces (exported worklist in; results sheet written back and
  re-read from disk), 25+ executed actions, and is classified ``VERIFIED``
  under the governed standard profile with ZERO model calls;
- a billed order with no ledger entry routes to an explicit HALT terminal
  (never auto-created);
- ambiguous duplicate ledger entries and a stale reconciliation snapshot are
  refused at the UI gateway BEFORE anything is written;
- a phantom results-sheet write (acknowledged but never persisted) is CAUGHT
  by the governed arm re-reading the file and silently accepted by the
  banner-oracle arm;
- the direct persisted-state adjudicator (SQLite + CSV reads) agrees.
"""

from __future__ import annotations

import json
from collections import Counter
from copy import deepcopy
from pathlib import Path

import pytest

from benchmark.o2c_recon import ground_truth
from benchmark.o2c_recon.run import ARMS, SCENARIOS, run_benchmark


@pytest.fixture(scope="module")
def results() -> dict:
    return run_benchmark(n=1, log=lambda _m: None)


def _rows(results: dict, arm: str, scenario: str) -> list[dict]:
    return [r for r in results["runs"] if r["arm"] == arm and r["scenario"] == scenario]


def test_every_scenario_ran_under_both_arms(results):
    for arm in ARMS:
        for scenario in SCENARIOS:
            assert _rows(results, arm, scenario), (arm, scenario)


def test_zero_model_calls_everywhere(results):
    assert all(int(r["model_calls"] or 0) == 0 for r in results["runs"])


def test_every_executed_step_used_the_api_tier(results):
    for r in results["runs"]:
        if r["executed_action_steps"]:
            assert r["actuation_kinds"] == ["api"], r


def test_healthy_path_shape(results):
    for arm in ARMS:
        row = _rows(results, arm, "healthy")[0]
        assert row["worklist_rows"] == 10
        assert 25 <= row["executed_action_steps"] <= 60
        assert row["reported_success"] is True
        assert row["gt_correct"] is True


def test_governed_healthy_run_is_verified(results):
    row = _rows(results, "governed", "healthy")[0]
    assert row["execution_profile"] == "standard"
    assert row["governed_policy_name"].endswith("multiapp-standard.yaml")
    assert row["governed_approval_source"] == "benchmark-standard-run-gate"
    assert row["transaction_outcome"] == "VERIFIED"
    assert row["transaction_billable"] is True


def test_missing_ledger_entry_halts_at_the_explicit_terminal(results):
    for arm in ARMS:
        row = _rows(results, arm, "missing_in_ledger")[0]
        assert row["halted"] is True
        assert row["terminal_outcome"] == "halt"
        assert row["gt_correct"] is True, row["gt_violations"]
        # The row BEFORE the missing one was still processed safely.
        assert row["executed_action_steps"] == 2


@pytest.mark.parametrize("scenario", ["ambiguous_duplicate", "stale_snapshot"])
def test_conflicts_halt_and_route_to_reconciliation(results, scenario):
    # The UI gateway refuses the write, so nothing is persisted -- the direct
    # state oracle confirms zero deltas. But the RUNTIME cannot prove that: the
    # request WAS sent (``ActuationStatus.HALT`` is documented as "the write may
    # have landed") and no verifier read the system of record afterwards. The
    # taxonomy must therefore not claim a proven absence; an unverified
    # actuation routes to RECONCILIATION_REQUIRED.
    for arm in ARMS:
        row = _rows(results, arm, scenario)[0]
        assert row["halted"] is True
        assert row["gt_correct"] is True, row["gt_violations"]
        assert row["transaction_outcome"] == "RECONCILIATION_REQUIRED"
        assert row["transaction_outcome"] != "HALTED_BEFORE_EFFECT"
        assert all(delta == 0 for delta in row["table_deltas"].values()), row


def test_phantom_writeback_is_caught_by_reading_the_file(results):
    naive = _rows(results, "naive", "phantom_writeback")[0]
    governed = _rows(results, "governed", "phantom_writeback")[0]
    assert naive["silent_wrong"] is True
    assert any(v.startswith("writeback_row_missing") for v in naive["gt_violations"])
    assert governed["halted"] is True
    assert governed["silent_wrong"] is False
    assert governed["gt_correct"] is True


def test_headline_silent_wrong_counts(results):
    per_arm = results["metrics"]["per_arm"]
    assert per_arm["governed"]["silent_wrong"] == 0
    assert per_arm["governed"]["over_halts"] == 0
    assert per_arm["naive"]["silent_wrong"] >= 1


def test_direct_state_oracle_catches_unrelated_in_place_mutation():
    before = ground_truth.Snapshot(
        billing_tables={
            "billed_orders": [
                {
                    "order_id": "ORD-9301",
                    "customer": "Atlas Manufacturing",
                    "amount_billed": "520.00",
                    "period": "2026-06",
                }
            ]
        },
        ledger_tables={
            "ledger_entries": [
                {
                    "id": 1,
                    "order_id": "ORD-9301",
                    "customer": "Atlas Manufacturing",
                    "amount_posted": "500.00",
                    "status": "open",
                }
            ],
            "adjustments": [],
        },
        results_rows=[],
        export_sha256="fixture",
    )
    after = deepcopy(before)
    after.billing_tables["billed_orders"][0]["amount_billed"] = "999.00"
    verdict = ground_truth.judge("stale_snapshot", before, after, completed=False)
    assert (
        "unexpected_record_change:billing.billed_orders:ORD-9301" in verdict.violations
    )


def test_committed_n3_evidence_shape_and_headline():
    evidence = json.loads(
        (Path(__file__).parents[1] / "benchmark/o2c_recon/results.json").read_text()
    )
    assert evidence["n_per_scenario"] == 3
    cells = Counter((row["arm"], row["scenario"]) for row in evidence["runs"])
    assert cells == Counter(
        {(arm, scenario): 3 for arm in ARMS for scenario in SCENARIOS}
    )
    assert len(evidence["runs"]) == 30
    assert evidence["headline"] == {
        "governed_verified": 3,
        "governed_silent_wrong": 0,
        "governed_over_halts": 0,
        "naive_silent_wrong": 3,
        "model_calls_total": 0,
    }
