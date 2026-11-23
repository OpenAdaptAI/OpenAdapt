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
    changed_files: list[str],
    event_name: str = "pull_request",
    tmp_path: Path | None = None,
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
            assert re.search(r"@[0-9a-f]{40}$", ref), (
                f"unpinned action in {name}: {ref}"
            )


def test_packages_workflow_enables_loadscope_and_caches() -> None:
    wf = _load("packages.yml")
    jobs = wf["jobs"]

    unit_test = next(s for s in jobs["flow-unit"]["steps"] if s.get("name") == "Test")
    assert "pytest -n logical --dist loadscope" in unit_test["run"]

    unit_enc = next(
        s
        for s in jobs["flow-unit"]["steps"]
        if s.get("name")
        == "Rollback, checkpoint and key tests under the default (encrypted) mode"
    )
    assert "pytest -n logical --dist loadscope" in unit_enc["run"]

    at_rest_test = next(
        s
        for s in jobs["flow-default-at-rest"]["steps"]
        if s.get("name") == "Test under the default (encrypted) mode"
    )
    assert "pytest -n logical --dist loadscope" in at_rest_test["run"]

    onprem_uv = next(
        s
        for s in jobs["flow-onprem-offline"]["steps"]
        if "setup-uv@" in s.get("uses", "")
    )
    assert onprem_uv["with"]["enable-cache"] is True

    fw_steps = jobs["flow-agent-frameworks"]["steps"]
    pw_cache = next(s for s in fw_steps if s.get("name") == "Cache Playwright browsers")
    assert pw_cache["with"]["path"] == "~/.cache/ms-playwright"
    assert "55cc8345863c7cc4c66a329aec7e433d2d1c52a9" in pw_cache["uses"]


@pytest.mark.parametrize(
    ("changed_files", "expect_flow", "expect_at_rest", "expect_code"),
    [
        # Encryption/storage/durable/seal/harness/lock/types/workflow paths MUST select flow_at_rest
        (["packages/flow/openadapt_flow/crypto.py"], "true", "true", "true"),
        (["packages/flow/openadapt_flow/stream_aead.py"], "true", "true", "true"),
        (["packages/flow/openadapt_flow/key_rotation.py"], "true", "true", "true"),
        (["packages/flow/openadapt_flow/sealed_set.py"], "true", "true", "true"),
        (["packages/flow/openadapt_flow/retention.py"], "true", "true", "true"),
        (
            ["packages/flow/openadapt_flow/runtime/durable/resume.py"],
            "true",
            "true",
            "true",
        ),
        (["packages/flow/tests/conftest.py"], "true", "true", "true"),
        (["packages/flow/uv.lock"], "true", "true", "true"),
        (["packages/types/openadapt_types/models.py"], "true", "true", "true"),
        ([".github/workflows/packages.yml"], "true", "true", "true"),
        # Unrelated Flow code PRs skip the duplicate 4-shard flow-default-at-rest run
        # (flow-unit shard 1 still runs the 21 core encrypted tests, and push to main runs full suite)
        (["packages/flow/openadapt_flow/api.py"], "true", "false", "true"),
        (["packages/flow/openadapt_flow/compiler/distill.py"], "true", "false", "true"),
        (["packages/flow/chrome_extension/content.js"], "true", "false", "true"),
        (["packages/capture/openadapt_capture/recorder.py"], "true", "false", "true"),
        # Prose-only Flow PRs skip all heavy sub-package jobs
        (["packages/flow/docs/LIMITS.md"], "true", "false", "false"),
        (
            ["packages/flow/README.md", "packages/flow/CHANGELOG.md"],
            "true",
            "false",
            "false",
        ),
        (["packages/flow/docs/commands/run.md"], "true", "false", "false"),
        # Mixed prose + non-storage code selects flow_onprem/agent_frameworks but not flow_at_rest
        (
            ["packages/flow/docs/LIMITS.md", "packages/flow/openadapt_flow/api.py"],
            "true",
            "false",
            "true",
        ),
        # Unrelated package PR selects none
        (["packages/tray/README.md"], "false", "false", "false"),
    ],
)
def test_classifier_selects_all_flow_code_and_skips_prose_only_prs(
    tmp_path: Path,
    changed_files: list[str],
    expect_flow: str,
    expect_at_rest: str,
    expect_code: str,
) -> None:
    out = _run_classifier(changed_files, event_name="pull_request", tmp_path=tmp_path)
    assert out["flow"] == expect_flow
    assert out["flow_at_rest"] == expect_at_rest
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
                "flow-default-at-rest": "success",
                "flow-onprem-offline": "success",
                "flow-agent-frameworks": "success",
            },
            0,
        ),
        # Push to main fails gate if flow-default-at-rest was skipped when selected
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
        # Scheduled nightly run skips flow-default-at-rest cleanly (not_in_nightly)
        (
            "schedule",
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


def test_desktop_replay_link_check_and_protected_safety_phase2_invariants() -> None:
    desktop_replay = _load("desktop-replay.yml")
    dr_paths = _triggers(desktop_replay)["pull_request"]["paths"]
    assert "packages/flow/openadapt_flow/**" not in dr_paths
    assert "packages/flow/openadapt_flow/backends/**" in dr_paths
    assert "packages/flow/openadapt_flow/runtime/**" in dr_paths

    link_check = _load("link-check.yml")
    for event in ("pull_request", "push"):
        lc_paths = _triggers(link_check)[event]["paths"]
        assert "!**/CHANGELOG.md" in lc_paths
        assert "!.github/**/*.md" in lc_paths
        assert "!legacy/**/*.md" in lc_paths

    psr = _load("protected-safety-review.yml")
    conc = psr["concurrency"]
    assert "github.event.pull_request.number" in conc["group"]
    assert "github.event.issue.number" in conc["group"]
    assert conc["cancel-in-progress"] is False

    pkg_jobs = _load("packages.yml")["jobs"]
    assert "fast-checks" in pkg_jobs
    for removed in (
        "capture-contract",
        "privacy-contract",
        "tray-contract",
        "desktop-release-intent",
    ):
        assert removed not in pkg_jobs
    assert "fast-checks" in pkg_jobs["packages-gate"]["needs"]

    pm_jobs = _load("platform-manifest.yml")["jobs"]
    assert "check-source-boundary" not in pm_jobs
    step_names = [s.get("name") for s in pm_jobs["validate-platform-manifest"]["steps"]]
    assert "Check open-core source boundary" in step_names


def test_fast_checks_classifier_selection(tmp_path: Path) -> None:
    assert (
        _run_classifier(["packages/capture/README.md"], "pull_request", tmp_path)[
            "fast_checks"
        ]
        == "true"
    )
    assert (
        _run_classifier(["packages/privacy/README.md"], "pull_request", tmp_path)[
            "fast_checks"
        ]
        == "true"
    )
    assert (
        _run_classifier(["packages/tray/README.md"], "pull_request", tmp_path)[
            "fast_checks"
        ]
        == "true"
    )
    assert (
        _run_classifier(["packages/desktop/README.md"], "pull_request", tmp_path)[
            "fast_checks"
        ]
        == "true"
    )
    assert (
        _run_classifier(
            ["packages/flow/openadapt_flow/api.py"], "pull_request", tmp_path
        )["fast_checks"]
        == "false"
    )
    assert (
        _run_classifier(["README.md"], "pull_request", tmp_path)["fast_checks"]
        == "false"
    )
