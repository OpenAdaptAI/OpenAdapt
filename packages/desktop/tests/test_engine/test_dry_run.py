"""A dry-run claim requires retained non-delivery plus affirmative Flow checks."""

from __future__ import annotations

import copy

import pytest

from openadapt_desktop.dry_run import dry_run_evidence
from tests.test_engine.test_dispatch import _precise_report


def _report():
    report = _precise_report("HALTED", production_eligible=False, execution_completed=False)
    report["transaction_outcome"] = "HALTED_BEFORE_EFFECT"
    report["results"] = [
        {
            "step_id": "save",
            "delivery_attempted": False,
            "safety_halt": True,
            "effect_contract_hashes": ["a" * 64],
        }
    ]
    report["approval"] = {
        "mode": "not_gated",
        "decision": "stop_before_saving",
        "decision_reason": "dry_run",
        "needs_person": True,
        "gated_step_id": "save",
        "pre_checks": [
            {"code": "right_record", "passed": True, "step_id": "save"},
            {
                "code": "dry_run",
                "passed": True,
                "detail": "stopped_before_delivery",
                "step_id": "save",
            },
        ],
    }
    report["approval"]["pre_checks"] += [
        {"code": "dry_run", "passed": True, "detail": detail, "step_id": "save"}
        for detail in ("step_gates_passed", "no_input_delivered")
    ]
    return report


def test_complete_dry_evidence_projects_codes_without_values():
    report = _report()
    report["raw_values"] = {"password": "synthetic-private-value"}
    evidence = dry_run_evidence(report)
    assert evidence["reached_boundary"] is True
    assert "synthetic-private-value" not in str(evidence)


@pytest.mark.parametrize(
    "mutation",
    [
        "delivered",
        "unknown",
        "missing",
        "wrong_outcome",
        "wrong_gate",
        "failed_check",
        "wrong_step",
        "missing_gate_check",
    ],
)
def test_incomplete_or_contradictory_dry_evidence_refuses(mutation):
    report = copy.deepcopy(_report())
    if mutation in {"delivered", "unknown"}:
        report["results"][0]["delivery_attempted"] = True if mutation == "delivered" else None
    elif mutation == "missing":
        report.pop("approval")
    elif mutation == "wrong_outcome":
        report["transaction_outcome"] = "RECONCILIATION_REQUIRED"
    elif mutation == "wrong_gate":
        report["approval"]["decision_reason"] = "each_item"
    elif mutation == "failed_check":
        report["approval"]["pre_checks"][0]["passed"] = False
    elif mutation == "wrong_step":
        report["approval"]["gated_step_id"] = "another-step"
    elif mutation == "missing_gate_check":
        report["approval"]["pre_checks"] = report["approval"]["pre_checks"][:-1]
    assert dry_run_evidence(report)["reached_boundary"] is False
