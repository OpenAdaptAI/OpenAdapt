from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_current_flow_local_benchmark.py"
SPEC = importlib.util.spec_from_file_location("current_flow_local_benchmark", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _row(
    *,
    outcome: str,
    steady: float,
    end_to_end: float,
    success: bool,
    reported: bool,
) -> dict:
    return {
        "arm": "compiled",
        "primary_outcome": outcome,
        "steady_wall_s": steady,
        "end_to_end_wall_s": end_to_end,
        "success": success,
        "replayer_success": reported,
        "wrong_action": False,
        "api_calls": 0,
        "cost_usd": 0.0,
    }


def test_aggregate_reports_nearest_rank_and_failure_taxonomy() -> None:
    rows = [
        _row(
            outcome="correct",
            steady=1.0,
            end_to_end=2.0,
            success=True,
            reported=True,
        ),
        _row(
            outcome="silent_incorrect_success",
            steady=2.0,
            end_to_end=3.5,
            success=False,
            reported=True,
        ),
        _row(
            outcome="halt_or_error",
            steady=3.0,
            end_to_end=5.0,
            success=False,
            reported=False,
        ),
    ]

    result = MODULE._aggregate(rows)

    assert result["n"] == 3
    assert result["task_success_count"] == 1
    assert result["silent_incorrect_success_count"] == 1
    assert result["halt_or_error_count"] == 1
    assert result["steady_wall_s_median"] == 2.0
    assert result["steady_wall_s_p95_nearest_rank"] == 3.0
    assert result["end_to_end_wall_s_p95_nearest_rank"] == 5.0
    assert result["model_calls_total"] == 0
    assert result["model_cost_usd_total"] == 0.0


def test_classification_never_trusts_actor_completion_as_oracle() -> None:
    assert (
        MODULE._classify({"arm": "compiled", "success": False, "replayer_success": True})
        == "silent_incorrect_success"
    )
    assert (
        MODULE._classify({"arm": "compiled", "success": True, "replayer_success": False})
        == "over_halt"
    )


def test_rejects_less_than_three_counted_trials(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least three"):
        MODULE.run_benchmark(
            tmp_path,
            tmp_path / "flow.whl",
            tmp_path / "out",
            trials=2,
        )


def test_wheel_extraction_rejects_path_traversal(tmp_path: Path) -> None:
    wheel = tmp_path / "unsafe.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("../escaped.py", "bad")

    with pytest.raises(ValueError, match="unsafe wheel member"):
        MODULE._extract_wheel(wheel, tmp_path / "extract")


def test_reproduce_command_binds_the_measured_release() -> None:
    assert MODULE._reproduce_command("1.24.0", "openadapt_flow-1.24.0-py3-none-any.whl") == (
        "python scripts/run_current_flow_local_benchmark.py "
        "--flow-source <v1.24.0-checkout> "
        "--flow-wheel <openadapt_flow-1.24.0-py3-none-any.whl> "
        "--out <new-output-directory>"
    )
