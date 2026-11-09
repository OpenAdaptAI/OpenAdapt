from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "qualify_macos_ax_release.py"
SPEC = importlib.util.spec_from_file_location("macos_ax_release", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
qualification = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qualification)


def _candidate() -> dict:
    return {
        "git_sha": "a" * 40,
        "git_tree": "b" * 40,
        "git_dirty": False,
        "dirty_paths": [],
        "flow_version": "1.20.0",
    }


def _passing_report() -> dict:
    return {
        "lane": "release",
        "automatic_retry": False,
        "environment": {"candidate": _candidate()},
        "oracle": "exact target-file bytes",
        "failure_taxonomy": ["identity_false_accept", "cleanup_failure"],
        "healthy_trials": [
            {
                "trial": trial,
                "status": "passed",
                "report_success": True,
                "all_steps_ok": True,
                "oracle": {"status": "confirmed"},
                "resolution_rungs": ["structural"],
                "identity": {"status": "verified", "mode": "structured"},
                "model_calls": 0,
            }
            for trial in range(1, 4)
        ],
        "wrong_identity_trials": [
            {
                "trial": trial,
                "status": "passed",
                "report_success": False,
                "pre_write_halt": True,
                "oracle": {"status": "confirmed"},
                "identity": {"status": "mismatch", "mode": "structured"},
                "model_calls": 0,
            }
            for trial in range(1, 4)
        ],
        "ambiguity_trials": [
            {
                "trial": trial,
                "status": "passed",
                "report_success": False,
                "refused": True,
                "oracles": [{"status": "confirmed"}, {"status": "confirmed"}],
                "model_calls": 0,
            }
            for trial in range(1, 4)
        ],
        "cleanup_errors": [],
    }


def test_release_matrix_is_fixed_at_three_trials_per_condition() -> None:
    assert qualification.HEALTHY_TRIALS == 3
    assert qualification.WRONG_IDENTITY_TRIALS == 3
    assert qualification.AMBIGUITY_TRIALS == 3


def test_evaluator_accepts_exact_clean_zero_error_matrix() -> None:
    result = qualification.evaluate_report(_passing_report())
    assert result["accepted"] is True
    assert result["decision"] == "accepted"
    assert result["matrix"] == {
        "healthy_exact_effect_trials": 3,
        "one_glyph_wrong_identity_pre_write_halts": 3,
        "ambiguous_window_pre_write_halts": 3,
        "exact_fixed_matrix": True,
        "healthy_semantics": True,
        "wrong_identity_semantics": True,
        "ambiguity_semantics": True,
    }
    assert result["metrics"] == {
        "silent_incorrect_successes": 0,
        "over_halts": 0,
        "false_completions": 0,
        "writes_after_refusal": 0,
        "model_calls": 0,
    }


def test_evaluator_rejects_dirty_or_unbound_candidate() -> None:
    report = _passing_report()
    report["environment"]["candidate"]["git_dirty"] = True
    assert qualification.evaluate_report(report)["accepted"] is False

    report = _passing_report()
    report["environment"]["candidate"]["git_sha"] = "short"
    assert qualification.evaluate_report(report)["accepted"] is False


def test_evaluator_rejects_fewer_than_three_trials() -> None:
    report = _passing_report()
    report["wrong_identity_trials"].pop()
    result = qualification.evaluate_report(report)
    assert result["accepted"] is False
    assert result["matrix"]["exact_fixed_matrix"] is False


def test_evaluator_counts_silent_incorrect_success_and_over_halt() -> None:
    report = _passing_report()
    report["healthy_trials"][0]["oracle"]["status"] = "refuted"
    report["healthy_trials"][0]["status"] = "failed"
    report["healthy_trials"][1]["report_success"] = False
    report["healthy_trials"][1]["status"] = "failed"
    result = qualification.evaluate_report(report)
    assert result["accepted"] is False
    assert result["metrics"]["silent_incorrect_successes"] == 1
    assert result["metrics"]["over_halts"] == 1


def test_evaluator_rejects_false_completion_or_write_after_refusal() -> None:
    report = _passing_report()
    report["wrong_identity_trials"][0]["report_success"] = True
    report["wrong_identity_trials"][0]["oracle"]["status"] = "refuted"
    result = qualification.evaluate_report(report)
    assert result["accepted"] is False
    assert result["metrics"]["false_completions"] == 1
    assert result["metrics"]["writes_after_refusal"] == 1


def test_evaluator_rejects_model_calls_cleanup_errors_and_retries() -> None:
    report = _passing_report()
    report["healthy_trials"][0]["model_calls"] = 1
    report["cleanup_errors"] = ["PID remained"]
    report["automatic_retry"] = True
    result = qualification.evaluate_report(report)
    assert result["accepted"] is False
    assert result["metrics"]["model_calls"] == 1
    assert result["cleanup_errors"] == ["PID remained"]
