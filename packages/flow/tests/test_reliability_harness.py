"""Small synthetic checks for the public reliability aggregation mechanism."""

from openadapt_flow.benchmark.reliability import _outcome, aggregate


def _result(outcome: str, *, category: str | None = None) -> dict:
    return {
        "compile_success": outcome != "compile_error",
        "outcome": outcome,
        "failure_category": category,
        "root_cause_hint": None,
    }


def test_outcome_requires_oracle_agreement_for_success() -> None:
    assert _outcome(True, True) == "success"
    assert _outcome(True, False) == "wrong_action"
    assert _outcome(False, True) == "false_halt"
    assert _outcome(False, False) == "safe_halt"


def test_aggregate_preserves_safety_failure_classes() -> None:
    summary = aggregate(
        [
            _result("success"),
            _result("safe_halt", category="identity_mismatch"),
            _result("wrong_action", category="wrong_action_silent"),
        ]
    )

    assert summary["n_apps"] == 3
    assert summary["n_compiled"] == 3
    assert summary["outcomes"] == {
        "success": 1,
        "safe_halt": 1,
        "wrong_action": 1,
    }
    assert summary["wrong_action_count"] == 1
    assert summary["safe_halt_count"] == 1
    assert summary["failure_categories"] == {
        "identity_mismatch": 1,
        "wrong_action_silent": 1,
    }
