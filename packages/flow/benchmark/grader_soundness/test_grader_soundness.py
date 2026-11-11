"""Offline tests for the valid-alternative families and the summary, on the
kit's toy payments target (no benchmark install needed)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run as gs_run  # noqa: E402
from alternatives import (  # noqa: E402
    AltContext,
    audit_alternative,
    refused_attempt,
    reordered_writes,
)
from openadapt_flow.verifier_audit.families import Refusal  # noqa: E402
from openadapt_flow.verifier_audit.targets.toy import ToyPayments  # noqa: E402


def _ctx(target, task):
    ref = target.reference(task)
    return AltContext(task=task, reference=ref), target.run(task, ref, score=False)


def test_reordered_writes_keeps_the_end_state_and_both_scorers_accept():
    for strict in (False, True):
        target = ToyPayments(strict=strict)
        ctx, control = _ctx(target, "pay-carol-and-dave")
        cell = audit_alternative(target, ctx.task, "reordered_writes", ctx, control)
        assert cell["status"] == "ACCEPTED"
        assert cell["effect_diff"] == {"missing": [], "extra": []}


def test_single_write_task_refuses_reorder():
    target = ToyPayments()
    ctx, _ = _ctx(target, "pay-bob")
    assert isinstance(reordered_writes(ctx), Refusal)


def test_refused_attempt_counts_only_when_the_environment_refuses_it():
    target = ToyPayments(strict=True)
    ctx, control = _ctx(target, "pay-bob")
    cands = refused_attempt(ctx)
    assert cands and "nonexistent" in json.dumps(
        [c.to_json() for c in cands[0].calls]
    )
    cell = audit_alternative(target, ctx.task, "refused_attempt", ctx, control)
    assert cell["status"] == "ACCEPTED"
    assert cell["census_attempts"][0]["calls_as_expected"] is True


def test_self_corrected_create_is_not_equivalent_and_not_counted():
    # The toy store appends, so a wrong send plus a right send leaves two
    # payments: the end state differs and the cell is never scored.
    target = ToyPayments(strict=True)
    ctx, control = _ctx(target, "pay-bob")
    cell = audit_alternative(target, ctx.task, "self_corrected_write", ctx, control)
    assert cell["status"] == "NOT_EQUIVALENT"
    assert "verdict" not in cell


def test_end_to_end_run_and_summary(tmp_path):
    out = tmp_path / "results" / "toy" / "lenient"
    rc = gs_run.main(
        [
            "run",
            "--target",
            "openadapt_flow.verifier_audit.targets.toy:make_target",
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    summary_path = tmp_path / "results.json"
    gs_run.main(
        ["summarize", "--results", str(tmp_path / "results"), "--out", str(summary_path)]
    )
    bench = json.loads(summary_path.read_text())["benchmarks"]["toy"]
    rows = {r["family"]: r for r in bench["families"]}
    # The lenient toy scorer checks only recipient and amount.
    assert rows["duplicate"]["hits"] == rows["duplicate"]["n"] == 3
    assert rows["unauthorized_side_effect"]["hits"] == 3
    assert rows["reordered_writes"]["n"] == 1
    assert bench["headline"]["false_reject_pooled"]["hits"] == 0
    assert bench["headline"]["false_accept_pooled"]["n"] > 0
    assert all(c["status"] == "FALSE_ACCEPT" for c in bench["counterexamples"])
