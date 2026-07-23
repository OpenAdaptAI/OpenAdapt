"""Headless evidence-contract tests for the Citrix stand-in qualification."""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HARNESS = (
    REPO / "benchmark" / "citrix_workspace" / "run_citrix_workspace_qualification.py"
)
SPEC = importlib.util.spec_from_file_location("citrix_workspace_qualification", HARNESS)
assert SPEC is not None and SPEC.loader is not None
qualification = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = qualification
SPEC.loader.exec_module(qualification)

CANDIDATE = "a" * 40
BASE = "b" * 40


def _clean_git(*args: str) -> str:
    outputs = {
        ("rev-parse", "HEAD"): CANDIDATE,
        ("merge-base", CANDIDATE, "origin/main"): BASE,
        ("status", "--porcelain", "--untracked-files=no"): "",
    }
    return outputs[args]


def _healthy(condition_trial: int) -> dict:
    return {
        "condition_trial": condition_trial,
        "passed": True,
        "model_calls": 0,
        "effect_confirmed": True,
        "structural_rung_used": 0,
        "visual_rungs_used": {"template": 3},
        "silent_incorrect_success": False,
        "false_completion": False,
        "over_halt": False,
    }


def _drift(condition_trial: int) -> dict:
    return {
        "condition_trial": condition_trial,
        "passed": True,
        "model_calls": 0,
        "halted": True,
        "silent_write": False,
        "silent_incorrect_success": False,
        "false_completion": False,
        "over_halt": False,
    }


def test_source_provenance_binds_clean_head_and_merge_base(monkeypatch) -> None:
    monkeypatch.setattr(qualification, "_git_output", _clean_git)
    qualification._validate_source_provenance(CANDIDATE, BASE)


@pytest.mark.parametrize(
    ("candidate", "base", "message"),
    (
        ("abc123", BASE, "candidate commit must be a full"),
        (CANDIDATE, "B" * 40, "base commit must be a full"),
    ),
)
def test_source_provenance_requires_full_lowercase_shas(
    monkeypatch,
    candidate: str,
    base: str,
    message: str,
) -> None:
    monkeypatch.setattr(qualification, "_git_output", _clean_git)
    with pytest.raises(RuntimeError, match=message):
        qualification._validate_source_provenance(candidate, base)


def test_source_provenance_refuses_candidate_not_at_head(monkeypatch) -> None:
    monkeypatch.setattr(
        qualification,
        "_git_output",
        lambda *args: "c" * 40 if args == ("rev-parse", "HEAD") else "",
    )
    with pytest.raises(RuntimeError, match="does not match checkout HEAD"):
        qualification._validate_source_provenance(CANDIDATE, BASE)


def test_source_provenance_refuses_wrong_merge_base(monkeypatch) -> None:
    def wrong_base(*args: str) -> str:
        if args == ("merge-base", CANDIDATE, "origin/main"):
            return "c" * 40
        return _clean_git(*args)

    monkeypatch.setattr(qualification, "_git_output", wrong_base)
    with pytest.raises(RuntimeError, match="does not match origin/main merge-base"):
        qualification._validate_source_provenance(CANDIDATE, BASE)


def test_source_provenance_refuses_tracked_modifications(monkeypatch) -> None:
    def dirty_tree(*args: str) -> str:
        if args == ("status", "--porcelain", "--untracked-files=no"):
            return " M openadapt_flow/backends/citrix_workspace.py"
        return _clean_git(*args)

    monkeypatch.setattr(qualification, "_git_output", dirty_tree)
    with pytest.raises(RuntimeError, match="tracked modifications"):
        qualification._validate_source_provenance(CANDIDATE, BASE)


def test_code_readiness_requires_fail_closed_three_plus_three() -> None:
    healthy = [_healthy(index) for index in range(1, 4)]
    drift = [_drift(index) for index in range(1, 4)]
    assert qualification._code_readiness_accepted(healthy, drift)

    for collection, index, key, value in (
        (healthy, 0, "passed", False),
        (healthy, 1, "silent_incorrect_success", True),
        (healthy, 2, "over_halt", True),
        (drift, 0, "silent_write", True),
        (drift, 1, "false_completion", True),
        (drift, 2, "passed", False),
    ):
        broken_healthy = copy.deepcopy(healthy)
        broken_drift = copy.deepcopy(drift)
        target = broken_healthy if collection is healthy else broken_drift
        target[index][key] = value
        assert not qualification._code_readiness_accepted(broken_healthy, broken_drift)

    assert not qualification._code_readiness_accepted(healthy[:2], drift)
    assert not qualification._code_readiness_accepted(healthy, drift[:2])
