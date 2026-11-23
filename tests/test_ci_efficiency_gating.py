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


def _run_classifier(
    changed_files: list[str], event_name: str = "pull_request", tmp_path: Path | None = None
) -> dict[str, str]:
    """Run the actual `Classify the change` bash step with `git diff` stubbed."""
    classify_step = next(
        s
        for s in _load("packages.yml")["jobs"]["scope"]["steps"]
        if s.get("name") == "Classify the change"
    )
    script = classify_step["run"]
    assert 'changed="$(git diff --name-only "$base" HEAD)"' in script
    stubbed = script.replace(
        'changed="$(git diff --name-only "$base" HEAD)"',
        'changed="$STUB_CHANGED"',
    )
    out_file = (tmp_path / "github_output.txt") if tmp_path else None
    assert out_file is not None
    out_file.write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "EVENT_NAME": event_name,
        "BEFORE_SHA": "",
        "GROUP_BASE_SHA": "",
        "STUB_CHANGED": "\n".join(changed_files),
        "GITHUB_OUTPUT": str(out_file),
    }
    res = subprocess.run(
        ["bash", "-c", stubbed],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    outputs: dict[str, str] = {}
    for line in out_file.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            outputs[k.strip()] = v.strip()
    return outputs


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

    unit_enc = next(
        s
        for s in jobs["flow-unit"]["steps"]
        if s.get("name") == "Rollback, checkpoint and key tests under the default (encrypted) mode"
    )
    assert "pytest -n logical --dist loadscope" in unit_enc["run"]

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
    ("changed_files", "expect_flow", "expect_code"),
    [
        # #2423-style encryption/storage/runtime/compiler/test/lock paths MUST select
        (["packages/flow/openadapt_flow/crypto.py"], "true", "true"),
        (["packages/flow/openadapt_flow/stream_aead.py"], "true", "true"),
        (["packages/flow/openadapt_flow/key_rotation.py"], "true", "true"),
        (["packages/flow/openadapt_flow/retention.py"], "true", "true"),
        (["packages/flow/openadapt_flow/compiler/distill.py"], "true", "true"),
        (["packages/flow/tests/conftest.py"], "true", "true"),
        (["packages/flow/uv.lock"], "true", "true"),
        (["packages/types/openadapt_types/models.py"], "true", "true"),
        (["packages/capture/openadapt_capture/recorder.py"], "true", "true"),
        ([".github/workflows/packages.yml"], "true", "true"),
        # Prose-only Flow PRs skip the heavy encrypted/on-prem/framework jobs
        (["packages/flow/docs/LIMITS.md"], "true", "false"),
        (["packages/flow/README.md", "packages/flow/CHANGELOG.md"], "true", "false"),
        (["packages/flow/docs/commands/run.md"], "true", "false"),
        # Mixed prose + code MUST select
        (
            ["packages/flow/docs/LIMITS.md", "packages/flow/openadapt_flow/api.py"],
            "true",
            "true",
        ),
        # Unrelated package PR selects neither
        (["packages/tray/README.md"], "false", "false"),
    ],
)
def test_classifier_selects_all_flow_code_and_skips_prose_only_prs(
    tmp_path: Path,
    changed_files: list[str],
    expect_flow: str,
    expect_code: str,
) -> None:
    out = _run_classifier(changed_files, event_name="pull_request", tmp_path=tmp_path)
    assert out["flow"] == expect_flow
    assert out["flow_at_rest"] == expect_code
    assert out["flow_onprem"] == expect_code
    assert out["flow_agent_frameworks"] == expect_code


@pytest.mark.parametrize(
    ("event", "outputs", "job_results", "expected"),
    [
        # Prose-only Flow PR: at-rest, onprem, and agent-frameworks skipped -> gate passes
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
        # Code-changing PR: flow_at_rest=true but skipped -> gate FAILS
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
        # On-prem selected but skipped -> gate FAILS
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
        # Cancelled job on an unselected PR STILL fails the gate
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
                "flow-default-at-rest": "cancelled",
                "flow-onprem-offline": "skipped",
                "flow-agent-frameworks": "skipped",
            },
            1,
        ),
        # Merge queue skips flow-default-at-rest cleanly (pr_only)
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
        # Push to main skips flow-default-at-rest cleanly (pr_only, as on main)
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
            0,
        ),
    ],
)
def test_packages_gate_enforces_sub_package_flow_selection(
    event: str,
    outputs: dict[str, str],
    job_results: dict[str, str],
    expected: int,
) -> None:
    gate_step = next(
        s
        for s in _load("packages.yml")["jobs"]["packages-gate"]["steps"]
        if s.get("name") == "Require every selected package job to pass"
    )
    gate = gate_step["run"]
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
