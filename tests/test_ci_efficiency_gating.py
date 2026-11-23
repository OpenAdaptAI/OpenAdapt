"""Contract tests for PR CI path-gating, xdist loadscope, caches, and CodeQL triggers."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _triggers(workflow: dict) -> dict:
    return workflow.get("on", workflow.get(True))


def test_codeql_runs_post_merge_and_scheduled_not_on_pull_request() -> None:
    wf = _load("codeql.yml")
    triggers = set(_triggers(wf))
    assert "pull_request" not in triggers
    assert {"push", "schedule", "workflow_dispatch"}.issubset(triggers)
    assert wf["concurrency"]["cancel-in-progress"] is False


def test_packages_workflow_pins_every_action_to_40_char_sha() -> None:
    for name in ("packages.yml", "codeql.yml"):
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        uses = re.findall(r"uses:\s*(\S+)", text)
        assert uses, name
        for ref in uses:
            if ref.startswith("./"):
                continue
            assert re.search(r"@[0-9a-f]{40}$", ref), f"unpinned action in {name}: {ref}"


def test_packages_workflow_enables_loadscope_and_caches() -> None:
    wf = _load("packages.yml")
    jobs = wf["jobs"]

    unit_test = next(s for s in jobs["flow-unit"]["steps"] if s.get("name") == "Test")
    assert "pytest -n logical --dist loadscope" in unit_test["run"]

    at_rest_test = next(
        s
        for s in jobs["flow-default-at-rest"]["steps"]
        if s.get("name") == "Test under the default (encrypted) mode"
    )
    assert "pytest -n logical --dist loadscope" in at_rest_test["run"]

    onprem_uv = next(
        s for s in jobs["flow-onprem-offline"]["steps"] if "setup-uv@" in s.get("uses", "")
    )
    assert onprem_uv["with"]["enable-cache"] is True

    fw_steps = jobs["flow-agent-frameworks"]["steps"]
    pw_cache = next(s for s in fw_steps if s.get("name") == "Cache Playwright browsers")
    assert pw_cache["with"]["path"] == "~/.cache/ms-playwright"
    assert "55cc8345863c7cc4c66a329aec7e433d2d1c52a9" in pw_cache["uses"]


@pytest.mark.parametrize(
    ("event", "outputs", "job_results", "expected"),
    [
        # Unrelated Flow PR: at-rest, onprem, and agent-frameworks skipped -> gate passes
        (
            "pull_request",
            {
                "flow": "true",
                "flow_at_rest": "false",
                "flow_onprem": "false",
                "flow_agent_frameworks": "false",
            },
            {
                "flow-unit": "success",
                "flow-critical": "success",
                "flow-default-at-rest": "skipped",
                "flow-onprem-offline": "skipped",
                "flow-agent-frameworks": "skipped",
            },
            0,
        ),
        # Encryption/storage PR: flow_at_rest=true but skipped -> gate FAILS
        (
            "pull_request",
            {
                "flow": "true",
                "flow_at_rest": "true",
                "flow_onprem": "false",
                "flow_agent_frameworks": "false",
            },
            {
                "flow-unit": "success",
                "flow-critical": "success",
                "flow-default-at-rest": "skipped",
                "flow-onprem-offline": "skipped",
                "flow-agent-frameworks": "skipped",
            },
            1,
        ),
        # On-prem PR: flow_onprem=true but skipped -> gate FAILS
        (
            "pull_request",
            {
                "flow": "true",
                "flow_at_rest": "false",
                "flow_onprem": "true",
                "flow_agent_frameworks": "false",
            },
            {
                "flow-unit": "success",
                "flow-critical": "success",
                "flow-default-at-rest": "skipped",
                "flow-onprem-offline": "skipped",
                "flow-agent-frameworks": "skipped",
            },
            1,
        ),
        # Merge queue skips flow-default-at-rest cleanly even when flow_at_rest=true
        (
            "merge_group",
            {
                "flow": "true",
                "flow_at_rest": "true",
                "flow_onprem": "false",
                "flow_agent_frameworks": "false",
            },
            {
                "flow-unit": "skipped",
                "flow-critical": "success",
                "flow-default-at-rest": "skipped",
                "flow-onprem-offline": "skipped",
                "flow-agent-frameworks": "skipped",
            },
            0,
        ),
        # Push to main requires flow-default-at-rest when flow_at_rest=true
        (
            "push",
            {
                "flow": "true",
                "flow_at_rest": "true",
                "flow_onprem": "true",
                "flow_agent_frameworks": "true",
            },
            {
                "flow-unit": "success",
                "flow-critical": "success",
                "flow-default-at-rest": "skipped",
                "flow-onprem-offline": "success",
                "flow-agent-frameworks": "success",
            },
            1,
        ),
    ],
)
def test_packages_gate_enforces_sub_package_flow_selection(
    event: str,
    outputs: dict[str, str],
    job_results: dict[str, str],
    expected: int,
) -> None:
    gate = _load("packages.yml")["jobs"]["packages-gate"]["steps"][1]["run"]
    script = "\n".join(gate.splitlines()[1:-1])
    needs: dict[str, dict] = {"scope": {"result": "success", "outputs": outputs}}
    for name, status in job_results.items():
        needs[name] = {"result": status}
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "RESULTS": json.dumps(needs), "EVENT_NAME": event},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == expected, result.stdout + result.stderr
