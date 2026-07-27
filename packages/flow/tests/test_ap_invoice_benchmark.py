"""CI guard for the AP invoice multi-system benchmark.

Runs the REAL harness once (n=1; deterministic, localhost only, zero model
calls) and pins the qualitative claims the committed
``benchmark/ap_invoice/results.json`` publishes, so they can never silently
regress:

- the healthy path is a 30+ action, 2-application, branching workflow that
  the governed standard-profile stack classifies ``VERIFIED`` (email
  confirmation verified by reading the outbox maildir; payments via the REST
  oracle; ERP rows via read-only SQL) with ZERO model calls;
- the missing-PO and ambiguous-duplicate intakes HALT safely with no
  persisted effect (``HALTED_BEFORE_EFFECT``);
- the collateral adjacent-record overwrite is CAUGHT by the governed arm and
  silently accepted by the banner-oracle arm (the honest differentiator);
- the payment-confirmation outage routes to ``RECONCILIATION_REQUIRED`` and a
  retry under the same idempotency key is SUPPRESSED (``REJECTED_POLICY``),
  never double-paid;
- the direct persisted-state adjudicator (SQLite + maildir reads) agrees.
"""

from __future__ import annotations

import json
from collections import Counter
from copy import deepcopy
from pathlib import Path

import pytest

from benchmark.ap_invoice import ground_truth
from benchmark.ap_invoice.run import ARMS, SCENARIOS, run_benchmark


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


def test_healthy_path_is_long_branching_and_multi_row(results):
    for arm in ARMS:
        row = _rows(results, arm, "healthy")[0]
        assert row["worklist_rows"] == 5
        assert 25 <= row["executed_action_steps"] <= 60
        assert row["reported_success"] is True
        assert row["gt_correct"] is True


def test_governed_healthy_run_is_verified_and_billable(results):
    row = _rows(results, "governed", "healthy")[0]
    assert row["execution_profile"] == "standard"
    assert row["governed_policy_name"].endswith("multiapp-standard.yaml")
    assert row["governed_approval_source"] == "benchmark-standard-run-gate"
    assert row["transaction_outcome"] == "VERIFIED"
    assert row["transaction_billable"] is True
    assert row["execution_outcome"] == "VERIFIED"


def test_naive_healthy_run_is_never_a_production_success(results):
    row = _rows(results, "naive", "healthy")[0]
    assert row["transaction_outcome"] == "COMPLETED_UNVERIFIED"
    assert row["transaction_billable"] is False


@pytest.mark.parametrize("scenario", ["missing_po", "duplicate_invoice"])
def test_entry_exceptions_halt_and_route_to_reconciliation(results, scenario):
    # Nothing consequential persisted -- the direct state oracle confirms zero
    # deltas. But the RUNTIME cannot prove that: the API request WAS sent
    # (``ActuationStatus.HALT`` is documented as "the write may have landed")
    # and no verifier read the system of record afterwards. Claiming a proven
    # absence here would tell an operator to reconcile nothing on evidence that
    # does not exist, so the unverified actuation routes to
    # RECONCILIATION_REQUIRED instead.
    for arm in ARMS:
        row = _rows(results, arm, scenario)[0]
        assert row["halted"] is True
        assert row["gt_correct"] is True, row["gt_violations"]
        assert row["transaction_outcome"] == "RECONCILIATION_REQUIRED"
        assert row["transaction_outcome"] != "HALTED_BEFORE_EFFECT"
        # Nothing consequential persisted anywhere (echo banner excluded).
        assert all(delta == 0 for delta in row["table_deltas"].values()), row


def test_collateral_overwrite_is_caught_by_governed_and_silent_under_naive(results):
    naive = _rows(results, "naive", "collateral_approve")[0]
    governed = _rows(results, "governed", "collateral_approve")[0]
    assert naive["silent_wrong"] is True
    assert "adjacent_invoice_modified" in naive["gt_violations"]
    assert governed["caught"] is True
    assert governed["silent_wrong"] is False
    assert governed["transaction_outcome"] == "RECONCILIATION_REQUIRED"


def test_uncertain_payment_routes_to_reconciliation_and_never_double_pays(results):
    governed = _rows(results, "governed", "payment_confirm_outage")[0]
    assert governed["transaction_outcome"] == "RECONCILIATION_REQUIRED"
    assert governed["halted"] is True
    # The retry under the same idempotency key was suppressed, not re-actuated.
    assert governed["retry_suppressed"] is True
    assert governed["retry_transaction_outcome"] == "REJECTED_POLICY"
    # Ground truth: exactly one payment landed; no duplicate.
    assert governed["gt_correct"] is True, governed["gt_violations"]


def test_headline_silent_wrong_counts(results):
    per_arm = results["metrics"]["per_arm"]
    assert per_arm["governed"]["silent_wrong"] == 0
    assert per_arm["governed"]["over_halts"] == 0
    assert per_arm["naive"]["silent_wrong"] >= 1


def test_direct_state_oracle_catches_unrelated_record_insert(tmp_path):
    before = ground_truth.Snapshot(
        tables={
            "vendors": [{"vendor_id": "V-100", "name": "Acme Supply Co"}],
            "invoices": [
                {
                    "id": 1,
                    "invoice_id": "INV-2090",
                    "vendor_id": "V-100",
                    "po_number": "PO-506",
                    "amount": "450.00",
                    "doc_sha256": "",
                    "status": "draft",
                    "discount_applied": "none",
                    "amount_payable": "",
                }
            ],
        },
        outbox={},
    )
    after = deepcopy(before)
    after.tables["vendors"].append({"vendor_id": "V-999", "name": "Unrelated Vendor"})
    verdict = ground_truth.judge(
        "missing_po", before, after, outbox_dir=tmp_path, completed=False
    )
    assert "unexpected_record_change:vendors:V-999" in verdict.violations


def test_committed_n3_evidence_shape_and_headline():
    evidence = json.loads(
        (Path(__file__).parents[1] / "benchmark/ap_invoice/results.json").read_text()
    )
    assert evidence["n_per_scenario"] == 3
    cells = Counter((row["arm"], row["scenario"]) for row in evidence["runs"])
    assert cells == Counter(
        {(arm, scenario): 3 for arm in ARMS for scenario in SCENARIOS}
    )
    assert len(evidence["runs"]) == 30
    assert (
        sum(row["retry_transaction_outcome"] is not None for row in evidence["runs"])
        == 6
    )
    assert evidence["headline"] == {
        "governed_verified": 3,
        "governed_silent_wrong": 0,
        "governed_over_halts": 0,
        # 12, not 6: `missing_po` and `duplicate_invoice` (3 runs each) route
        # here now. The API gateway refuses the write and the state oracle
        # confirms zero deltas, but the request WAS sent and no verifier read
        # the ledger, so the runtime cannot claim a proven absence.
        "governed_reconciliation_required": 12,
        "governed_suppressed_retries": 3,
        "naive_silent_wrong": 3,
        "model_calls_total": 0,
    }
