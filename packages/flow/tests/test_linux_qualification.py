from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "qualify_linux_atspi.py"
SPEC = importlib.util.spec_from_file_location("linux_atspi_qualification", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
qualification = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qualification)


def _clean(*, delivered: bool = True, oracle: str = "confirmed") -> dict:
    return {
        "backend_sequence_reported_success": delivered,
        "effect_oracle": {"status": oracle},
        "delivery_receipts": [
            {
                "operation": "atspi_focus",
                "native": True,
                "outcome_verified": False,
            },
            {
                "operation": "atspi_invoke",
                "native": True,
                "outcome_verified": False,
            },
        ],
    }


def _refusal(*, refused: bool = True, oracle: str = "confirmed") -> dict:
    return {"refused": refused, "effect_oracle": {"status": oracle}}


def test_qualification_counts_exactly_three_trials_per_condition() -> None:
    assert qualification.TRIALS_PER_CONDITION == 3


def test_exact_file_oracle_is_independent_and_byte_exact(tmp_path: Path) -> None:
    path = tmp_path / "effect"
    expected = b"expected"
    assert qualification.exact_file_oracle(path, expected)["status"] == "refuted"
    path.write_bytes(b"different")
    assert qualification.exact_file_oracle(path, expected)["status"] == "refuted"
    path.write_bytes(expected)
    assert qualification.exact_file_oracle(path, expected)["status"] == "confirmed"


def test_absence_oracle_refutes_any_external_effect(tmp_path: Path) -> None:
    path = tmp_path / "effect"
    assert qualification.absent_file_oracle(path)["status"] == "confirmed"
    path.write_bytes(b"unexpected")
    assert qualification.absent_file_oracle(path)["status"] == "refuted"


def test_metrics_separate_silent_incorrect_success_and_over_halt() -> None:
    metrics = qualification.clean_metrics(
        [
            _clean(),
            _clean(oracle="refuted"),
            _clean(delivered=False, oracle="refuted"),
        ]
    )
    assert metrics == {
        "effects_confirmed": 1,
        "silent_incorrect_successes": 1,
        "over_halts": 1,
    }


def test_acceptance_requires_three_clean_effects_and_all_refusals() -> None:
    clean = [_clean() for _ in range(3)]
    refusals = [_refusal() for _ in range(3)]
    metrics = qualification.evaluate(clean, refusals, refusals)
    assert metrics == {
        "accepted": True,
        "clean_trials": 3,
        "clean_effects_confirmed": 3,
        "silent_incorrect_successes": 0,
        "over_halts": 0,
        "native_delivery_only_receipts": 6,
        "ambiguity_refusals_confirmed": 3,
        "stale_target_refusals_confirmed": 3,
        "refusal_condition_failures": 0,
        "operator_interventions": 0,
        "model_calls": 0,
    }


def test_acceptance_rejects_unsafe_or_incomplete_refusal() -> None:
    clean = [_clean() for _ in range(3)]
    ambiguity = [_refusal(), _refusal(), _refusal(refused=False)]
    stale = [_refusal(), _refusal(), _refusal(oracle="refuted")]
    metrics = qualification.evaluate(clean, ambiguity, stale)
    assert metrics["accepted"] is False
    assert metrics["refusal_condition_failures"] == 2


def test_acceptance_rejects_receipt_that_claims_outcome_or_is_not_native() -> None:
    clean = [_clean() for _ in range(3)]
    clean[0]["delivery_receipts"][0]["outcome_verified"] = True
    clean[1]["delivery_receipts"][1]["native"] = False
    refusals = [_refusal() for _ in range(3)]
    metrics = qualification.evaluate(clean, refusals, refusals)
    assert metrics["accepted"] is False
    assert metrics["native_delivery_only_receipts"] == 4
