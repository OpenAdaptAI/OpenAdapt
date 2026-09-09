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
import os
import subprocess
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
    measured = json.loads(output.read_text(encoding="utf-8"))
    for trial in measured["trials"]:
        if trial["condition"] == "moderate_display_drift":
            report_path = (
                work_root
                / "trials"
                / trial["condition"]
                / f"trial-{trial['trial']:02d}"
                / "run"
                / "report.json"
            )
            trial["native_report"] = json.loads(report_path.read_text(encoding="utf-8"))
    return measured


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


def test_compact_result_handles_fail_closed_harness_failure() -> None:
    result = {
        "schema_version": "openadapt.qualification-gate-results.v1",
        "accepted_subset": False,
        "stopped_early": True,
        "harness_failure": {
            "exception_type": "RuntimeError",
            "message": "the external durable authority is unavailable",
        },
    }

    assert mod.compact_result(result) == {
        "accepted_subset": False,
        "harness_failure": result["harness_failure"],
    }


def test_harness_failure_returns_fail_closed_result_without_summary_key_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "failure.json"
    work_root = tmp_path / "failure-work"

    def fail_before_campaign(_recording_dir: Path) -> None:
        raise RuntimeError("the external durable authority is unavailable")

    monkeypatch.setattr(mod, "_record_demo", fail_before_campaign)
    monkeypatch.setattr(
        sys,
        "argv",
        [str(RUNNER), "--output", str(output), "--work-root", str(work_root)],
    )
    code = mod.main()

    result = json.loads(output.read_text(encoding="utf-8"))
    captured = capsys.readouterr()
    assert code == 1
    assert result["accepted_subset"] is False
    assert result["stopped_early"] is True
    assert result["harness_failure"] == {
        "exception_type": "RuntimeError",
        "message": "the external durable authority is unavailable",
    }
    assert '"accepted_subset": false' in captured.out
    assert '"harness_failure"' in captured.out
    assert "campaign FAILED before completion" in captured.err
    assert "KeyError" not in captured.err


@pytest.mark.skipif(
    os.name == "nt" or not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="requires POSIX file permissions under an unprivileged user",
)
def test_cli_durable_authority_failure_preserves_the_original_failure(
    tmp_path: Path,
) -> None:
    output = tmp_path / "failure.json"
    work_root = tmp_path / "failure-work"
    unavailable_home = tmp_path / "unavailable-home"
    authority_dir = unavailable_home / ".openadapt" / "durable-authority"
    authority_dir.mkdir(parents=True)
    authority_db = authority_dir / "authority.sqlite3"
    authority_db.touch(mode=0o600)
    authority_db.chmod(0)
    env = os.environ.copy()
    env.pop("OPENADAPT_DURABLE_AUTHORITY_DB", None)
    env["HOME"] = str(unavailable_home)
    env["PYTHONPATH"] = str(REPO)
    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(RUNNER),
                "--output",
                str(output),
                "--work-root",
                str(work_root),
            ],
            cwd=REPO,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
        )
    finally:
        authority_db.chmod(0o600)

    result = json.loads(output.read_text(encoding="utf-8"))
    assert completed.returncode == 1
    assert result == {
        "accepted_subset": False,
        "campaign_contract": "benchmark/qualification_gate/campaign.json",
        "campaign_id": "qualification-gate-campaign-v1",
        "harness_failure": {
            "exception_type": "DurableAuthorityBusy",
            "message": "the external durable authority is unavailable",
        },
        "schema_version": "openadapt.qualification-gate-results.v1",
        "stopped_early": True,
    }
    assert "the external durable authority is unavailable" in completed.stderr
    assert '"accepted_subset": false' in completed.stdout
    assert '"harness_failure"' in completed.stdout
    assert "KeyError" not in completed.stderr


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
    verified_conditions = ("healthy", "row_reordered")
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


def _assert_moderate_display_contract(trial: dict) -> None:
    # The original campaign declares verified_or_safe_halt for this condition.
    # Each alternative still needs its independent effect and native evidence.
    assert trial["expectation"] == "verified_or_safe_halt"
    assert trial["passed"] is True
    report = trial["native_report"]
    assert report["success"] is trial["runtime_success"]
    assert report["transaction_outcome"] == trial["transaction_outcome"]
    assert report["idempotent_replay"] is False
    assert report["model_calls"] == trial["model_calls"] == 0
    assert trial["uncertainty_events"] == trial["delivery_uncertainty"] == []
    for step in report["results"]:
        assert step["input_retried"] is False
        assert step["delivery_uncertainty"] is None

    oracle = trial["oracle"]
    assert oracle["wrong_record_write"] is False
    assert oracle["duplicate_effect"] is False
    saves = [event for event in oracle["events"] if event["kind"] == "save_note"]
    assert oracle["save_entries"] == len(saves)
    assert sum(row["action"] == "save_note" for row in oracle["ledger"]) == len(saves)
    if trial["runtime_success"] is True:
        assert trial["transaction_outcome"] == "VERIFIED"
        assert trial["save_delivery_attempts"] == len(saves) == 1
        assert oracle["exact_single_write"] is True
        assert oracle["no_consequential_input"] is False
        assert oracle["rows"] == [
            {"record_id": mod.fx.TARGET_RECORD, "note": mod.fx.NOTE_VALUE}
        ]
        assert saves[0]["detail"] == mod.fx.TARGET_RECORD
    else:
        assert trial["runtime_success"] is False
        assert trial["transaction_outcome"] in {
            "HALTED_BEFORE_EFFECT",
            "RECONCILIATION_REQUIRED",
        }
        assert trial["save_delivery_attempts"] == len(saves) == 0
        assert oracle["rows"] == []
        assert oracle["exact_single_write"] is False
        assert oracle["no_consequential_input"] is True
        refusals = [
            {
                "step_id": step["step_id"],
                "stage": evidence["stage"],
                "code": evidence["code"],
                "delivery_attempted": step["delivery_attempted"],
            }
            for step in report["results"]
            if (evidence := step["safety_refusal_evidence"]) is not None
        ]
        assert trial["safety_refusals"] == refusals
        assert refusals
        for refusal in refusals:
            assert refusal["stage"] and refusal["code"]
            assert refusal["delivery_attempted"] is False
        assert all(
            step["delivery_attempted"] is False
            for step in report["results"]
            if step["risk"] == "irreversible"
        )
        assert trial["errors"] == [
            step["error"] for step in report["results"] if step["error"]
        ]
        assert trial["errors"]


def test_moderate_display_drift_proves_its_declared_alternative(results: dict) -> None:
    for trial in results["trials"]:
        if trial["condition"] == "moderate_display_drift":
            _assert_moderate_display_contract(trial)


def _moderate_display_observation(*, verified: bool) -> dict:
    """Small unit inputs for the evidence assertions, never campaign evidence."""
    outcome = "VERIFIED" if verified else "RECONCILIATION_REQUIRED"
    refusal = (
        None
        if verified
        else {"stage": "identity_verification", "code": "identity_conflict"}
    )
    error = None if verified else "Identity check refused before Save"
    event = {"kind": "save_note", "detail": mod.fx.TARGET_RECORD}
    return {
        "expectation": "verified_or_safe_halt",
        "passed": True,
        "runtime_success": verified,
        "transaction_outcome": outcome,
        "model_calls": 0,
        "save_delivery_attempts": int(verified),
        "uncertainty_events": [],
        "delivery_uncertainty": [],
        "safety_refusals": []
        if verified
        else [{"step_id": "save", **refusal, "delivery_attempted": False}],
        "errors": [] if verified else [error],
        "oracle": {
            "rows": [{"record_id": mod.fx.TARGET_RECORD, "note": mod.fx.NOTE_VALUE}]
            if verified
            else [],
            "events": [event] if verified else [],
            "ledger": [{"action": "save_note"}] if verified else [],
            "save_entries": int(verified),
            "exact_single_write": verified,
            "no_consequential_input": not verified,
            "wrong_record_write": False,
            "duplicate_effect": False,
        },
        "native_report": {
            "success": verified,
            "transaction_outcome": outcome,
            "idempotent_replay": False,
            "model_calls": 0,
            "results": [
                {
                    "step_id": "save",
                    "risk": "irreversible",
                    "input_retried": False,
                    "delivery_uncertainty": None,
                    "delivery_attempted": verified,
                    "safety_refusal_evidence": refusal,
                    "error": error,
                }
            ],
        },
    }


@pytest.mark.parametrize("verified", [True, False])
def test_moderate_display_contract_accepts_proven_alternatives(verified: bool) -> None:
    _assert_moderate_display_contract(_moderate_display_observation(verified=verified))


@pytest.mark.parametrize(
    ("verified", "path", "value"),
    [
        pytest.param(False, ("runtime_success",), True, id="false-success"),
        pytest.param(
            True, ("oracle", "exact_single_write"), False, id="missing-effect-proof"
        ),
        pytest.param(True, ("oracle", "rows"), [], id="missing-actual-effect"),
        pytest.param(
            True,
            ("oracle", "rows"),
            [{"record_id": "wrong", "note": mod.fx.NOTE_VALUE}],
            id="wrong-record-effect",
        ),
        pytest.param(True, ("save_delivery_attempts",), 2, id="duplicate-save"),
        pytest.param(
            False,
            ("oracle", "rows"),
            [{"record_id": mod.fx.TARGET_RECORD, "note": mod.fx.NOTE_VALUE}],
            id="halt-after-write",
        ),
        pytest.param(
            False, ("save_delivery_attempts",), 1, id="halt-after-save-attempt"
        ),
        pytest.param(
            False,
            ("oracle", "events"),
            [{"kind": "save_note", "detail": mod.fx.TARGET_RECORD}],
            id="halt-after-oracle-input",
        ),
        pytest.param(
            False,
            ("native_report", "results", 0, "delivery_attempted"),
            True,
            id="halt-after-native-delivery",
        ),
        pytest.param(
            False,
            ("native_report", "results", 0, "input_retried"),
            True,
            id="blind-retry",
        ),
        pytest.param(False, ("native_report", "idempotent_replay"), True, id="replay"),
        pytest.param(False, ("safety_refusals",), [], id="missing-refusal"),
        pytest.param(
            False, ("safety_refusals", 0, "code"), "different", id="altered-refusal"
        ),
        pytest.param(
            False,
            ("native_report", "transaction_outcome"),
            "VERIFIED",
            id="altered-outcome",
        ),
        pytest.param(False, ("errors",), [], id="missing-error"),
    ],
)
def test_moderate_display_contract_rejects_unproven_alternatives(
    verified: bool, path: tuple, value: object
) -> None:
    trial = _moderate_display_observation(verified=verified)
    target = trial
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(AssertionError):
        _assert_moderate_display_contract(trial)


@pytest.mark.parametrize(
    ("verified", "changes"),
    [
        pytest.param(
            True,
            [
                (("transaction_outcome",), "COMPLETED_UNVERIFIED"),
                (("native_report", "transaction_outcome"), "COMPLETED_UNVERIFIED"),
            ],
            id="matching-unverified-success",
        ),
        pytest.param(
            False,
            [
                (("transaction_outcome",), "VERIFIED"),
                (("native_report", "transaction_outcome"), "VERIFIED"),
            ],
            id="matching-verified-halt",
        ),
        pytest.param(
            False,
            [
                (("safety_refusals", 0, "delivery_attempted"), True),
                (("native_report", "results", 0, "delivery_attempted"), True),
            ],
            id="matching-dispatched-refusal",
        ),
    ],
)
def test_moderate_display_contract_rejects_consistent_unsafe_claims(
    verified: bool, changes: list
) -> None:
    trial = _moderate_display_observation(verified=verified)
    for path, value in changes:
        target = trial
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
    with pytest.raises(AssertionError):
        _assert_moderate_display_contract(trial)
