"""The nightly full run: every package's suite against main, and one issue.

nightly.yml calls Packages (which calls the Desktop test and build workflows)
and the launcher CI on a schedule or dispatch, the tier that runs every job
whatever changed. scripts/ci/nightly_report.py then opens, updates or closes a
single "main is red" issue.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _triggers(workflow: dict) -> dict:
    # PyYAML reads the bare key `on` as True.
    return workflow.get("on", workflow.get(True))


NIGHTLY = _load("nightly.yml")


def _reporter():
    spec = importlib.util.spec_from_file_location(
        "nightly_report", ROOT / "scripts" / "ci" / "nightly_report.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_it_runs_nightly_and_on_dispatch() -> None:
    triggers = _triggers(NIGHTLY)
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert triggers["schedule"] == [{"cron": "23 7 * * *"}]


def test_it_calls_every_package_suite() -> None:
    jobs = NIGHTLY["jobs"]
    assert jobs["packages"]["uses"] == "./.github/workflows/packages.yml"
    assert jobs["launcher"]["uses"] == "./.github/workflows/main.yml"
    for name in ("packages.yml", "main.yml"):
        assert "workflow_call" in _triggers(_load(name)), name
    # Packages calls Desktop's suites, so the nightly run reaches them too.
    packages = _load("packages.yml")["jobs"]
    called = {job.get("uses") for job in packages.values()}
    assert {
        "./.github/workflows/desktop-test.yml",
        "./.github/workflows/desktop-build.yml",
    } <= called


def test_the_pr_lane_triggers_are_unchanged() -> None:
    for name in ("packages.yml", "main.yml"):
        triggers = _triggers(_load(name))
        assert {"pull_request", "merge_group", "push", "workflow_dispatch"} <= set(
            triggers
        )


def test_its_group_differs_from_the_groups_its_callees_compute() -> None:
    # Called workflows see this run's github context. main.yml's group is
    # `<workflow>-<run_id>`, so the caller must not hold that same group.
    group = NIGHTLY["concurrency"]["group"]
    assert "${{" not in group
    assert NIGHTLY["concurrency"]["cancel-in-progress"] is False


def test_the_report_runs_after_failures_but_not_after_a_cancel() -> None:
    report = NIGHTLY["jobs"]["report"]
    assert set(report["needs"]) == {"packages", "launcher"}
    assert report["if"] == "${{ !cancelled() && github.ref == 'refs/heads/main' }}"
    assert report["permissions"] == {
        "actions": "read",
        "contents": "read",
        "issues": "write",
    }
    assert any(
        "nightly_report.py" in (step.get("run") or "") for step in report["steps"]
    )
    assert NIGHTLY["permissions"] == {"contents": "read"}


@pytest.mark.parametrize("event", ["schedule", "workflow_dispatch"])
def test_packages_selects_every_package_on_a_nightly_event(
    event: str, tmp_path: Path
) -> None:
    # Runs the real scope step: a nightly event has no `before` commit.
    steps = _load("packages.yml")["jobs"]["scope"]["steps"]
    script = next(step["run"] for step in steps if step.get("id") == "classify")
    output = tmp_path / "out"
    env = {
        **os.environ,
        "EVENT_NAME": event,
        "BEFORE_SHA": "",
        "GROUP_BASE_SHA": "",
        "GITHUB_OUTPUT": str(output),
    }
    subprocess.run(
        ["bash", "-c", script], cwd=ROOT, env=env, check=True, capture_output=True
    )
    selected = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert selected and set(selected.values()) == {"true"}, selected
    assert {"flow", "types", "agent", "capture", "privacy", "desktop", "tray"} <= set(
        selected
    )


def test_only_unfinished_or_passing_jobs_are_left_out() -> None:
    reporter = _reporter()
    jobs = [
        {"name": "packages / types", "conclusion": "success"},
        {"name": "packages / flow unit", "conclusion": "failure"},
        {"name": "packages / tray (windows)", "conclusion": "timed_out"},
        {"name": "packages / capture (macos)", "conclusion": "cancelled"},
        {"name": "packages / skipped", "conclusion": "skipped"},
        {"name": "main is red issue", "conclusion": None},
    ]
    assert [job["name"] for job in reporter.failing_jobs(jobs)] == [
        "packages / flow unit",
        "packages / tray (windows)",
        "packages / capture (macos)",
    ]


def test_the_issue_body_links_the_run_and_each_failing_job() -> None:
    reporter = _reporter()
    body = reporter.render(
        [
            {
                "name": "packages / flow unit",
                "html_url": "https://x/job/1",
                "conclusion": "failure",
            }
        ],
        "https://x/run/9",
        "0123456789abcdef",
    )
    assert "[nightly full run](https://x/run/9)" in body
    assert "(0123456789ab)" in body
    assert "in 1 job:" in body
    assert "- [packages / flow unit](https://x/job/1): failure" in body


def test_the_reporter_opens_updates_and_closes_one_issue(
    monkeypatch, tmp_path: Path
) -> None:
    reporter = _reporter()
    calls: list[tuple[str, ...]] = []
    state = {"jobs": [], "issue": None}
    monkeypatch.setattr(reporter, "_jobs", lambda *a: state["jobs"])
    monkeypatch.setattr(reporter, "_open_issue", lambda repo: state["issue"])
    monkeypatch.setattr(reporter, "_gh", lambda *args: calls.append(args) or "")
    for key, value in {
        "GITHUB_REPOSITORY": "o/r",
        "GITHUB_RUN_ID": "9",
        "GITHUB_SHA": "0123456789abcdef",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
    }.items():
        monkeypatch.setenv(key, value)

    failed = {"name": "packages / flow unit", "html_url": "u", "conclusion": "failure"}
    state["jobs"] = [failed]
    reporter.main()
    assert [call[:2] for call in calls] == [("label", "create"), ("issue", "create")]
    assert "packages / flow unit" in (tmp_path / "summary.md").read_text()

    calls.clear()
    state["issue"] = 41
    reporter.main()
    assert [call[:3] for call in calls] == [
        ("issue", "edit", "41"),
        ("issue", "comment", "41"),
    ]

    calls.clear()
    state["jobs"] = [{**failed, "conclusion": "success"}]
    reporter.main()
    assert [call[:3] for call in calls] == [("issue", "close", "41")]

    calls.clear()
    state["issue"] = None
    reporter.main()
    assert calls == []
