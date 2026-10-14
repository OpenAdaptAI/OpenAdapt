from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "extract_over_halt_regression.py"
SPEC = importlib.util.spec_from_file_location("extract_over_halt_regression", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _report() -> dict:
    def run(condition: str, trial: int, outcome: str) -> dict:
        return {
            "arm": "compiled",
            "condition": condition,
            "trial": trial,
            "primary_outcome": outcome,
            "replayer_success": outcome != "over_halt",
            "success": True,
            "first_failure": {"step": "step_010", "error": "region_stable"},
            "steady_wall_s": 12.0,
            "end_to_end_wall_s": 13.0,
            "note_sha256": "a" * 64,
            "final_screenshot_sha256": "b" * 64,
        }

    return {
        "arms": ["compiled", "dom"],
        "conditions": ["clean", "theme"],
        "trials_per_arm_condition": 3,
        "task": "MockMed triage",
        "oracle": "screenshot/OCR final-state check",
        "source": {
            "flow": {
                "version": "1.24.0",
                "release_tag": "v1.24.0",
                "artifact": {"filename": "openadapt_flow-1.24.0-py3-none-any.whl"},
            },
            "runner_sha256": "c" * 64,
        },
        "runs": [
            run("clean", 1, "correct"),
            run("clean", 2, "over_halt"),
            run("clean", 3, "over_halt"),
            run("theme", 1, "correct"),
        ],
    }


def test_extracts_only_the_requested_condition_over_halts() -> None:
    regression = MODULE.build_regression(_report(), condition="clean")

    assert regression["observed_count"] == 2
    assert [obs["trial"] for obs in regression["observations"]] == [2, 3]
    assert regression["counted_trials"] == 3


def test_clean_condition_carries_no_drift_query() -> None:
    assert MODULE.build_regression(_report(), condition="clean")["condition"]["query"] == ""
    assert (
        MODULE.build_regression(_report(), condition="theme")["condition"]["query"]
        == "?drift=theme"
    )


def test_a_condition_with_no_over_halt_yields_an_empty_artifact() -> None:
    """An empty artifact is the evidence that a prior regression is fixed."""

    regression = MODULE.build_regression(_report(), condition="theme")

    assert regression["observed_count"] == 0
    assert regression["observations"] == []


def test_unknown_condition_or_arm_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown condition"):
        MODULE.build_regression(_report(), condition="rename")
    with pytest.raises(ValueError, match="unknown arm"):
        MODULE.build_regression(_report(), condition="clean", arm="agent")


def test_reproduce_command_names_the_measured_release(tmp_path: Path) -> None:
    regression = MODULE.build_regression(_report(), condition="clean")

    assert "v1.24.0-checkout" in regression["reproduce"]
    assert "openadapt_flow-1.24.0-py3-none-any.whl" in regression["reproduce"]
