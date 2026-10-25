"""The lifecycle harness's bookkeeping, end to end on the in-memory app.

No browser, no fixture, no model: the toy arms in ``benchmark/lifecycle/fake.py``
drive ``FakeApp`` so the workload, the operator, the oracle, the metric and
the report can be checked in a second.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parents[1] / "benchmark"
sys.path.insert(0, str(BENCH))

from lifecycle import cost_model, metrics, run, workload  # noqa: E402
from lifecycle.arm import OperatorRequest  # noqa: E402
from lifecycle.operator import ScriptedOperator  # noqa: E402


def test_workload_is_seeded_and_covers_every_exception() -> None:
    a = workload.generate(7, 400, 10)
    b = workload.generate(7, 400, 10)
    assert workload.dumps(a) == workload.dumps(b)
    assert workload.dumps(workload.generate(8, 400, 10)) != workload.dumps(a)
    kinds = {t.exception for t in a.truth.values()}
    assert set(workload.EXCEPTION_MIX) <= kinds
    assert [d["kind"] for d in a.drift] == [
        "theme",
        "relabel",
        "reorder",
        "interstitial",
    ]
    # the arm never sees the truth
    for item in a.items:
        assert "truth" not in json.dumps(item.params)
    missing = [i for i in a.items if a.truth[i.item_id].exception == "missing_required"]
    assert missing and all("DOB" not in i.params for i in missing)


@pytest.mark.parametrize(
    "claimed,oracle,wanted,exception,expected",
    [
        ("done", "correct", True, "clean", "correct"),
        ("stopped", "correct", False, "dup_existing", "right_stop"),
        ("stopped", "absent", True, "clean", "false_stop"),
        ("stopped", "absent", True, "failed_save", "fault_stop"),
        ("stopped", "absent", True, "malformed_dob", "data_stop"),
        ("done", "absent", True, "clean", "silent_wrong"),
        ("done", "wrong", False, "dup_existing", "silent_wrong"),
        ("stopped", "wrong", True, "clean", "flagged_wrong"),
        ("stopped", "correct", True, "slow_save", "stopped_saved"),
    ],
)
def test_classify(claimed, oracle, wanted, exception, expected) -> None:
    assert (
        metrics.classify(
            claimed=claimed, oracle=oracle, chart_wanted=wanted, exception=exception
        )
        == expected
    )


def test_cost_model_bands_scale_every_charge() -> None:
    half = cost_model.V1.scaled(0.5)
    assert half.per_request("stop") == pytest.approx(
        0.5 * cost_model.V1.per_request("stop")
    )
    assert half.version.endswith("x0.5")
    assert "calibration" in cost_model.V1.to_dict()


def _operator(truth, look="absent"):
    rows = []
    op = ScriptedOperator(
        cost_model.V1,
        truth,
        look=lambda t: look,
        do_by_hand=lambda t: {"chart_id": "9"},
        drift_active=lambda: ["relabel"],
        record=rows.append,
    )
    return op, rows


def test_operator_policy() -> None:
    w = workload.generate(7, 200, 10)
    dup = next(t for t in w.truth.values() if t.exception == "dup_existing")
    clean = next(t for t in w.truth.values() if t.exception == "clean")
    op, rows = _operator(w.truth)
    assert op.ask(OperatorRequest("stop", dup.item_id, "dup", "test")).action == "skip"
    first = op.ask(
        OperatorRequest("stop", clean.item_id, "x", "test", {"resumable": True})
    )
    again = op.ask(
        OperatorRequest("stop", clean.item_id, "x", "test", {"resumable": True})
    )
    assert (first.action, again.action) == ("resume", "do_by_hand")
    assert (
        again.charged_s
        == cost_model.V1.per_request("stop") + cost_model.V1.manual_entry_s
    )
    ok = op.ask(
        OperatorRequest(
            "approval", clean.item_id, "save?", "test", {"values": dict(clean.intended)}
        )
    )
    bad = op.ask(
        OperatorRequest(
            "approval", dup.item_id, "save?", "test", {"values": dict(dup.person)}
        )
    )
    assert (ok.action, bad.action) == ("approve", "deny")
    repair = op.ask(
        OperatorRequest(
            "repair_review", None, "relabel", "test", {"probes_passed": True}
        )
    )
    assert repair.action == "accept_repair"
    assert all(r["cost_model"] == cost_model.V1.version for r in rows)
    op2, _ = _operator(w.truth, look="correct")
    assert (
        op2.ask(OperatorRequest("reconcile", clean.item_id, "?", "t")).action
        == "confirm_saved"
    )


def test_end_to_end_on_the_fake_app(tmp_path: Path) -> None:
    out = tmp_path / "run"
    assert (
        run.main(
            [
                "--app",
                "fake",
                "--arm",
                "lifecycle.fake:naive",
                "--arm",
                "lifecycle.fake:careful",
                "--items",
                "200",
                "--days",
                "10",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    res = json.loads((out / "results.json").read_text())
    naive, careful = res["arms"]["naive"], res["arms"]["careful"]
    # the naive arm claims everything done and the oracle catches its wrongs
    assert naive["silent_wrong_rate"]["k"] > 0
    assert naive["operator_requests"] == {}
    assert careful["silent_wrong_rate"]["k"] == 0
    assert careful["final_correct"] == careful["items"] == 200
    # a silent wrong is never a verified completion
    assert naive["verified_completed"] == naive["outcomes"]["correct"]
    for arm in (naive, careful):
        b = arm["bands"]
        assert (
            b["x0.5"]["verified_completed_per_operator_hour"]
            > b["x1"]["verified_completed_per_operator_hour"]
            > b["x2"]["verified_completed_per_operator_hour"]
        )
        assert (
            b["x1"]["verified_completed_per_operator_hour_with_cleanup"]
            <= b["x1"]["verified_completed_per_operator_hour"]
        )
    report = (out / "REPORT.md").read_text()
    assert "Verified completed items per operator-hour" in report
    assert "No timings from real operators" in report
    # the ledger alone reproduces the results
    from lifecycle.report import results

    assert json.loads(json.dumps(results(out), sort_keys=True)) == res


def test_a_stalled_item_times_out_and_the_run_goes_on(tmp_path: Path) -> None:
    out = tmp_path / "run"
    started = __import__("time").time()
    assert (
        run.main(
            [
                "--app",
                "fake",
                "--arm",
                "lifecycle.fake:stalling",
                "--items",
                "5",
                "--days",
                "1",
                "--item-timeout",
                "0.5",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    assert __import__("time").time() - started < 20  # never waited out the stall
    rows = [
        json.loads(line) for line in (out / "ledger.jsonl").read_text().splitlines()
    ]
    items = [r for r in rows if r["type"] == "item_result"]
    assert [r["seq"] for r in items] == [1, 2, 3, 4, 5]
    stalled = next(r for r in items if r["seq"] == 2)
    assert stalled["claimed"] == "error"
    assert stalled["product_status"].startswith("ItemTimedOut")
    assert [r["seq"] for r in rows if r["type"] == "item_timeout"] == [2]
    assert all(r["claimed"] != "error" for r in items if r["seq"] != 2)
