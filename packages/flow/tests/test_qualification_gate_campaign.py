"""Gate-standard behavioral contract for the local qualification campaign.

Runs the deterministic campaign once (module scope) and asserts the
production-gate evidence standard on its counted results: at least three
trials per condition, explicit silent-incorrect-success and over-halt counts,
and at least three expected uncertain-delivery fault trials that return
RECONCILIATION_REQUIRED (or a contract-proven VERIFIED after uncertainty)
with zero blind retries and zero replay dispatches.

No Docker, no network, no browser, no model calls.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
RUNNER = REPO / "benchmark" / "qualification_gate" / "run_campaign.py"
CONTRACT = REPO / "benchmark" / "qualification_gate" / "campaign.json"


def _load_runner():
    spec = importlib.util.spec_from_file_location("gate_runner_contract", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mod = _load_runner()


def _write_mutated_contract(tmp_path: Path, mutate) -> Path:
    contract = json.loads(CONTRACT.read_text())
    mutate(contract)
    path = tmp_path / "campaign.json"
    path.write_text(json.dumps(contract), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def results(tmp_path_factory: pytest.TempPathFactory) -> dict:
    root = tmp_path_factory.mktemp("gate-campaign")
    output = root / "results.json"
    work_root = root / "run"
    code = _run_main(output, work_root)
    assert code == 0, f"campaign refused acceptance (exit {code})"
    return json.loads(output.read_text(encoding="utf-8"))


def _run_main(output: Path, work_root: Path) -> int:
    """Invoke main() through its argv surface — the exact human entry point."""

    import contextlib
    import io as _io

    sys_argv_backup = sys.argv
    sys.argv = [str(RUNNER), "--output", str(output), "--work-root", str(work_root)]
    try:
        with contextlib.redirect_stdout(_io.StringIO()):
            code = mod.main()
    finally:
        sys.argv = sys_argv_backup
    return int(code)


CONTRACT_IDS = [
    str(item["id"])
    for item in json.loads(CONTRACT.read_text(encoding="utf-8"))["conditions"]
]


def test_campaign_contract_matches_the_implemented_matrix() -> None:
    specs = {spec.id: spec.expect for spec in mod.condition_specs()}
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    declared = {str(item["id"]): str(item["expect"]) for item in contract["conditions"]}
    assert declared == specs
    assert int(contract["trials_per_condition"]) >= 3
    assert int(contract["minimum_uncertain_delivery_conditions"]) >= 3


def test_required_metrics_include_the_explicit_failure_counts() -> None:
    for metric in ("silent_incorrect_successes", "over_halts"):
        assert metric in mod.REQUIRED_METRICS
    uncertain = [m for m in mod.REQUIRED_METRICS if "blind" in m or "dispatch" in m]
    assert sorted(uncertain) == ["blind_retries", "replay_dispatches"]


def test_summary_guard_fails_closed_when_a_counter_is_absent(
    results: dict,
) -> None:
    for metric in mod.REQUIRED_METRICS:
        tampered = dict(results)
        tampered.pop(metric)
        with pytest.raises(RuntimeError, match=metric):
            mod.assert_complete_summary(tampered)
    tampered = dict(results)
    tampered["silent_incorrect_successes"] = True  # non-int counter
    with pytest.raises(RuntimeError, match="silent_incorrect_successes"):
        mod.assert_complete_summary(tampered)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda c: c.__setitem__("trials_per_condition", 2),
            "at least three trials",
        ),
        (
            lambda c: c.__setitem__("required_metrics", ["verified_outcomes"]),
            "diverge from the harness",
        ),
        (
            lambda c: c["conditions"].pop(),
            "diverge from campaign.json",
        ),
        (
            lambda c: c["conditions"][0].__setitem__("expect", "safe_halt"),
            "changed_expectation",
        ),
        (
            lambda c: c.__setitem__("minimum_uncertain_delivery_conditions", 2),
            "at least three uncertain-delivery conditions",
        ),
    ],
)
def test_harness_refuses_a_diverged_contract(
    tmp_path: Path, mutation, message: str
) -> None:
    path = _write_mutated_contract(tmp_path, mutation)
    with pytest.raises(RuntimeError, match=message):
        mod._validate_against_contract(mod.condition_specs(), contract_path=path)


def test_full_campaign_meets_the_gate_standard(results: dict) -> None:
    assert results["schema_version"] == "openadapt.qualification-gate-results.v1"
    assert results["accepted_subset"] is True
    assert results["full_campaign_complete"] is True
    assert results["stopped_early"] is False
    assert results["run_count"] == 33
    assert results["trials_per_condition"] == 3
    counts = results["condition_trial_counts"]
    assert set(counts) == set(CONTRACT_IDS)
    assert all(count == 3 for count in counts.values())


def test_explicit_failure_counts_are_present_and_zero(results: dict) -> None:
    for metric in (
        "silent_incorrect_successes",
        "over_halts",
        "wrong_record_writes",
        "duplicate_effects",
        "model_calls",
        "blind_retries",
        "replay_dispatches",
    ):
        assert metric in results, f"counted summary lacks {metric}"
        assert results[metric] == 0, f"{metric} must be zero"


def test_uncertain_delivery_fault_trials_meet_the_gate(results: dict) -> None:
    uncertain_conditions = results["uncertain_delivery_conditions"]
    assert len(uncertain_conditions) >= 3
    counts = results["condition_trial_counts"]
    for condition in uncertain_conditions:
        assert counts[condition] >= 3, condition
    by_id = {}
    for trial in results["trials"]:
        by_id.setdefault(trial["condition"], []).append(trial)

    lost = by_id["uncertain_delivery_write_lost"]
    kept = by_id["uncertain_delivery_write_kept_timeout"]
    unreachable = by_id["uncertain_delivery_oracle_unreachable"]

    for trial in lost + unreachable:
        assert trial["passed"] is True
        assert trial["transaction_outcome"] == "RECONCILIATION_REQUIRED"
        assert trial["runtime_success"] is False
        assert trial["save_delivery_attempts"] == 1
        assert len(trial["uncertainty_events"]) == 1
        assert all(row["retried"] is False for row in trial["delivery_uncertainty"])
        assert all(
            row["effects_confirmed"] is not True
            for row in trial["delivery_uncertainty"]
        )

    for trial in kept:
        assert trial["passed"] is True
        assert trial["transaction_outcome"] == "VERIFIED"
        assert trial["save_delivery_attempts"] == 1
        assert all(row["retried"] is False for row in trial["delivery_uncertainty"])
        assert all(
            row["effects_confirmed"] is True and row["resolved_by_contract"] is True
            for row in trial["delivery_uncertainty"]
        )
        assert trial["oracle"]["exact_single_write"] is True


def test_halt_conditions_prove_no_effect_on_the_system_of_record(
    results: dict,
) -> None:
    halt_conditions = (
        "severe_display_drift",
        "duplicate_save_control",
        "partial_render",
        "wrong_record_before_write",
        "stale_identity_before_write",
    )
    by_id: dict[str, list[dict]] = {}
    for trial in results["trials"]:
        by_id.setdefault(trial["condition"], []).append(trial)
    for condition in halt_conditions:
        for trial in by_id[condition]:
            assert trial["passed"] is True, condition
            assert trial["runtime_success"] is False, condition
            assert trial["save_delivery_attempts"] == 0, condition
            oracle = trial["oracle"]
            assert oracle["rows"] == [], condition
            assert oracle["no_consequential_input"] is True, condition
            assert oracle["exact_single_write"] is False, condition


def test_verified_conditions_prove_one_exact_write(results: dict) -> None:
    verified_conditions = ("healthy", "row_reordered", "moderate_display_drift")
    by_id: dict[str, list[dict]] = {}
    for trial in results["trials"]:
        by_id.setdefault(trial["condition"], []).append(trial)
    for condition in verified_conditions:
        for trial in by_id[condition]:
            assert trial["passed"] is True, condition
            assert trial["runtime_success"] is True, condition
            assert trial["transaction_outcome"] == "VERIFIED", condition
            oracle = trial["oracle"]
            assert oracle["exact_single_write"] is True, condition
            assert oracle["wrong_record_write"] is False, condition
            assert oracle["duplicate_effect"] is False, condition


def test_resolution_never_uses_a_model_rung(results: dict) -> None:
    forbidden = ("model", "grounder", "vlm", "llm")
    for trial in results["trials"]:
        assert trial["model_calls"] == 0
        for rung in trial["resolution_rungs"]:
            lowered = rung.lower()
            assert not any(marker in lowered for marker in forbidden), (
                f"{trial['condition']}: resolution used {rung}"
            )
