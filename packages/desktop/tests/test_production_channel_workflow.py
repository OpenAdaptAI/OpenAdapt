"""Workflow contract for the Production channel admission jobs.

The Desktop script imports the canonical OpenAdaptAI/.github validator at run
time. That validator has its own pinned dependencies (for example
``cryptography``), so every job that runs it must install them from the exact
central checkout first. A bare ``setup-python`` fails with
``ModuleNotFoundError`` before it can read any admission.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "production-channel.yml"
CENTRAL_PATH = "production-lifecycle"
CENTRAL_REQUIREMENTS = f"{CENTRAL_PATH}/requirements/profile-consistency.txt"


def _jobs() -> dict:
    payload = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload["jobs"]


def _index(steps: list[dict], predicate, label: str) -> int:
    matches = [index for index, step in enumerate(steps) if predicate(step)]
    assert len(matches) == 1, f"expected exactly one {label} step, found {len(matches)}"
    return matches[0]


def _is_central_checkout(step: dict) -> bool:
    return str(step.get("uses", "")).startswith("actions/checkout@") and (
        step.get("with", {}).get("path") == CENTRAL_PATH
    )


def _installs_central_requirements(step: dict) -> bool:
    run = " ".join(str(step.get("run", "")).split())
    return "pip install" in run and (
        f"-r {CENTRAL_REQUIREMENTS}" in run or f"--requirement {CENTRAL_REQUIREMENTS}" in run
    )


def _runs_canonical_validator(step: dict) -> bool:
    return "scripts/production_release.py state" in str(step.get("run", ""))


@pytest.mark.parametrize("job_name", ["inspect", "promote"])
def test_canonical_validator_dependencies_install_before_the_validator_runs(
    job_name: str,
) -> None:
    steps = _jobs()[job_name]["steps"]
    checkout = _index(steps, _is_central_checkout, "central checkout")
    install = _index(steps, _installs_central_requirements, "central requirements install")
    validator = _index(steps, _runs_canonical_validator, "canonical validator")
    assert checkout < install < validator
