"""`ci` is the one required check, and it stays complete.

ci.yml runs on pull requests and in the merge queue. It calls packages.yml and
main.yml, which no longer run on those events themselves, so each pull request
runs every package job once. Its `ci` job fails unless every called job passed
or was not selected, and the names branch protection requires today report
the same result until protection names `ci`.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _triggers(workflow: dict) -> dict:
    return workflow.get("on", workflow.get(True))


CI = _load("ci.yml")


def test_ci_runs_on_pull_requests_and_the_queue() -> None:
    triggers = _triggers(CI)
    assert set(triggers) == {"pull_request", "merge_group"}
    assert "ready_for_review" in triggers["pull_request"]["types"]


def test_the_lane_workflows_run_only_through_ci_on_pull_requests() -> None:
    # A second pull_request trigger would run every job twice and report the
    # legacy names twice.
    for name in ("packages.yml", "main.yml"):
        triggers = set(_triggers(_load(name)))
        assert triggers == {"push", "workflow_dispatch", "workflow_call"}, name
    jobs = CI["jobs"]
    assert jobs["packages"]["uses"] == "./.github/workflows/packages.yml"
    assert jobs["launcher"]["uses"] == "./.github/workflows/main.yml"


def test_no_other_workflow_reports_a_name_ci_owns() -> None:
    owned = {"ci", "run-ci (ubuntu-latest, 3.12)", "packages-gate"}
    for path in sorted(WORKFLOWS.glob("*.yml")):
        if path.name in ("ci.yml", "packages.yml", "main.yml"):
            continue
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not {"pull_request", "merge_group"} & set(_triggers(workflow) or {}):
            continue
        names = {job.get("name", key) for key, job in workflow["jobs"].items()}
        assert not names & owned, (path.name, names & owned)


def test_ci_requires_every_job_and_always_reports() -> None:
    job = CI["jobs"]["ci"]
    assert job["name"] == "ci"
    assert job["if"] == "always()"
    assert set(job["needs"]) == {"packages", "launcher", "smoke"}
    script = next(step["run"] for step in job["steps"] if "RESULTS" in step.get("env", {}))
    assert 'need["result"] != "success"' in script


def test_the_legacy_names_mirror_ci() -> None:
    names = {}
    for key in ("legacy-run-ci", "legacy-packages-gate"):
        job = CI["jobs"][key]
        assert job["needs"] == "ci" and job["if"] == "always()"
        assert job["steps"][-1]["run"] == 'test "$RESULT" = success'
        names[key] = job["name"]
    assert set(names.values()) == {"run-ci (ubuntu-latest, 3.12)", "packages-gate"}


def test_its_group_cannot_collide_with_a_called_workflows_group() -> None:
    # main.yml's group is `${{ github.workflow }}-<pr>`, which is `CI-<pr>`
    # when ci.yml calls it, and GitHub compares group names case-insensitively.
    group = CI["concurrency"]["group"]
    assert not group.lower().startswith("ci-")
