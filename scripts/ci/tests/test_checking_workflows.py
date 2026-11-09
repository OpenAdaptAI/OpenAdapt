"""Checking-tool invocation and workflow contracts; no secret scans run here."""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "run_lychee", ROOT / "scripts/ci/run_lychee.py"
)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


@pytest.fixture
def synthetic_lychee(tmp_path, monkeypatch):
    tool_dir = tmp_path / "bin"
    tool_dir.mkdir()
    tool = tool_dir / "lychee"
    tool.write_text("""#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
Path("invocation.json").write_text(json.dumps(sys.argv[1:]))
if os.environ["SYNTHETIC_REPORT"] != "absent":
    Path(sys.argv[sys.argv.index("--output")+1]).write_text(os.environ["SYNTHETIC_REPORT"])
raise SystemExit(int(os.environ["SYNTHETIC_EXIT"]))
""")
    tool.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tool_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv(
        "LYCHEE_ARGS",
        "--offline --include-fragments --no-progress './**/*.md' 'space name.md'",
    )
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setenv("GITHUB_REPOSITORY", "synthetic/repository")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    return tmp_path


@pytest.mark.parametrize("code", [0, 1, 2, 3])
@pytest.mark.parametrize("report", ["| Total | 2 |\n", "| Total | 0 |\n", "absent"])
def test_actual_runner_preserves_failure_and_report_policy(
    synthetic_lychee, monkeypatch, code, report
):
    monkeypatch.setenv("SYNTHETIC_EXIT", str(code))
    monkeypatch.setenv("SYNTHETIC_REPORT", report)
    result = runner.main(["--fail-if-empty", "--job-summary"])
    expected = (code or 1) if report == "absent" else (1 if "| 0 |" in report else code)
    assert result == expected
    args = json.loads((synthetic_lychee / "invocation.json").read_text())
    assert args == [
        "--mode",
        "task",
        "--format",
        "markdown",
        "--output",
        "lychee/out.md",
        "--offline",
        "--include-fragments",
        "--no-progress",
        "./**/*.md",
        "space name.md",
    ]
    if report != "absent":
        summary = (synthetic_lychee / "summary.md").read_text()
        assert summary.startswith(report)
        assert ("No links were found" in summary) == ("| 0 |" in report)
        assert (
            "actions/runs/123?check_suite_focus=true"
            in (synthetic_lychee / "lychee/out.md").read_text()
        )
    else:
        assert not (synthetic_lychee / "summary.md").exists()


def test_internal_empty_report_is_allowed_without_a_job_summary(
    synthetic_lychee, monkeypatch
):
    monkeypatch.setenv("SYNTHETIC_EXIT", "0")
    monkeypatch.setenv("SYNTHETIC_REPORT", "| Total | 0 |\n")
    assert runner.main([]) == 0
    assert not (synthetic_lychee / "summary.md").exists()


def test_stale_report_cannot_cover_a_missing_output(synthetic_lychee, monkeypatch):
    tool_dir = synthetic_lychee / "bin"
    tool = tool_dir / "lychee"
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(tool_dir) + os.pathsep + os.environ["PATH"])
    output = synthetic_lychee / "lychee/out.md"
    output.parent.mkdir()
    output.write_text("| Total | 5 |\n")
    assert runner.main([]) == 1
    assert not output.exists()


def workflow(name):
    return yaml.load(
        (ROOT / f".github/workflows/{name}.yml").read_text(), Loader=yaml.BaseLoader
    )


def test_gitleaks_version_scope_and_required_check_are_preserved():
    flow = workflow("secret-scan")
    assert set(flow["on"]) == {"pull_request", "merge_group", "push"}
    assert flow["on"]["push"]["branches"] == ["main"]
    assert flow["permissions"] == {"contents": "read"}
    job = flow["jobs"]["gitleaks"]
    assert job["name"] == "gitleaks"
    assert job["if"] == "github.event.pull_request.draft != true"
    steps = job["steps"]
    assert steps[0]["with"]["fetch-depth"] == "0"
    install = next(step for step in steps if step.get("name") == "Install gitleaks")
    assert install["env"]["GITLEAKS_VERSION"] == "8.30.1"
    assert (
        install["env"]["GITLEAKS_SHA256"]
        == "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
    )
    assert "scripts/ci/install_checked_tool.py" in install["run"]
    assert '--sha256 "$GITLEAKS_SHA256" --member gitleaks' in install["run"]
    assert (
        steps[-1]["run"]
        == "gitleaks git . --config .gitleaks.toml --redact --no-banner --log-opts=HEAD"
    )
    assert all(not step.get("continue-on-error") for step in steps)


def test_both_lychee_jobs_use_the_same_pinned_validated_tool():
    flow = workflow("link-check")
    assert flow["env"]["LYCHEE_VERSION"] == "v0.24.2"
    assert (
        flow["env"]["LYCHEE_SHA256"]
        == "1f4e0ef7f6554a6ed33dd7ac144fb2e1bbed98598e7af973042fc5cd43951c9a"
    )
    assert set(flow["jobs"]) == {"internal", "external"}
    assert flow["permissions"] == {"contents": "read"}
    for name, job in flow["jobs"].items():
        install = next(
            step for step in job["steps"] if step.get("name") == "Install pinned Lychee"
        )
        assert (
            "lychee-${LYCHEE_VERSION}/lychee-x86_64-unknown-linux-gnu.tar.gz"
            in install["run"]
        )
        assert '--sha256 "$LYCHEE_SHA256"' in install["run"]
        assert "--member lychee-x86_64-unknown-linux-gnu/lychee" in install["run"]
        assert all(not step.get("continue-on-error") for step in job["steps"])
        check = job["steps"][-1]
        assert check["env"]["GITHUB_TOKEN"] == "${{ github.token }}"
        if name == "internal":
            assert check["if"] == install["if"] == "steps.targets.outputs.paths != ''"
            assert check["run"] == "python3 scripts/ci/run_lychee.py"
            assert check["env"]["LYCHEE_ARGS"].startswith(
                "--offline --include-fragments --no-progress "
            )
        else:
            assert (
                check["run"]
                == "python3 scripts/ci/run_lychee.py --fail-if-empty --job-summary"
            )
            raw = check["env"]["LYCHEE_ARGS"].replace("${{ env.LYCHEE_PATHS }}", "")
            assert shlex.split(raw) == [
                "--no-progress",
                "--max-concurrency",
                "8",
                "--timeout",
                "30",
                "--max-retries",
                "3",
                "--accept",
                "200..=299,403,429",
                "--exclude",
                r"^https?://(localhost|127\.0\.0\.1|0\.0\.0\.0)",
                "--exclude",
                r"^https://(app|api)\.openadapt\.ai",
                "--exclude",
                r"linkedin\.com",
            ]
    for event in ("pull_request", "push"):
        assert {
            "**/*.md",
            ".github/workflows/link-check.yml",
            "scripts/ci/install_checked_tool.py",
            "scripts/ci/run_lychee.py",
        } == set(flow["on"][event]["paths"])
    assert flow["jobs"]["internal"]["timeout-minutes"] == "10"
    assert flow["jobs"]["external"]["timeout-minutes"] == "20"
