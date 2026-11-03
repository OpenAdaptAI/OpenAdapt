"""Every package releases from its newest green run on main.

scripts/ci/newest_green.py finds the newest first-parent commit on main that
changed the package and requires that commit's latest successful push run,
with the package's own jobs run and passed. desktop-release.yml set the rule;
the package, program-graph and launcher release workflows run the script.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
REPO = "OpenAdaptAI/OpenAdapt"


def _script():
    spec = importlib.util.spec_from_file_location(
        "newest_green", ROOT / "scripts" / "ci" / "newest_green.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NG = _script()


def _run(**overrides) -> dict:
    run = {
        "id": 1,
        "head_sha": "a" * 40,
        "event": "push",
        "head_branch": "main",
        "conclusion": "success",
        "updated_at": "2026-10-28T10:00:00Z",
        "repository": {"full_name": REPO},
    }
    return {**run, **overrides}


def test_the_newest_successful_push_run_on_main_wins() -> None:
    sha = "a" * 40
    runs = [
        _run(id=1),
        _run(id=2, updated_at="2026-10-28T12:00:00Z"),
        _run(id=3, conclusion="cancelled", updated_at="2026-10-28T13:00:00Z"),
        _run(id=4, event="workflow_dispatch", updated_at="2026-10-28T14:00:00Z"),
        _run(id=5, head_branch="feature", updated_at="2026-10-28T15:00:00Z"),
        _run(
            id=6,
            repository={"full_name": "fork/OpenAdapt"},
            updated_at="2026-10-28T16:00:00Z",
        ),
        _run(id=7, head_sha="b" * 40, updated_at="2026-10-28T17:00:00Z"),
    ]
    assert NG.select_run(runs, sha, REPO)["id"] == 2


def test_a_cancelled_only_commit_has_no_green_run() -> None:
    # The Desktop 0.23.0 case: the commit's only push run was cancelled.
    assert NG.select_run([_run(conclusion="cancelled")], "a" * 40, REPO) is None


def test_a_green_run_must_have_run_and_passed_the_package_jobs() -> None:
    prefixes = ["flow core lint", "flow core unit"]
    ok = [
        {"name": "flow core lint (ruff, mypy)", "conclusion": "success"},
        {"name": "flow core unit (shard 1/2)", "conclusion": "success"},
        {"name": "flow core unit (shard 2/2)", "conclusion": "success"},
    ]
    assert NG.job_problems(ok, prefixes) == []
    skipped = [{**job, "conclusion": "skipped"} for job in ok]
    assert len(NG.job_problems(skipped, prefixes)) == 2
    one_failed = [*ok[:2], {**ok[2], "conclusion": "failure"}]
    assert NG.job_problems(one_failed, prefixes) == [
        "'flow core unit (shard 2/2)' ended failure"
    ]


def test_the_subject_is_the_newest_first_parent_commit_that_changed_the_package(
    tmp_path: Path,
) -> None:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(tmp_path), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "packages" / "flow").mkdir(parents=True)
    (tmp_path / "packages" / "flow" / "a.py").write_text("1")
    git("add", "-A")
    git("commit", "-qm", "flow")
    flow = git("rev-parse", "HEAD")
    # A merged branch: its inner commit touches flow, the merge is the subject.
    git("checkout", "-qb", "feature")
    (tmp_path / "packages" / "flow" / "a.py").write_text("2")
    git("commit", "-qam", "inner")
    git("checkout", "-q", "main")
    git("merge", "-q", "--no-ff", "-m", "merge", "feature")
    merge = git("rev-parse", "HEAD")
    (tmp_path / "README.md").write_text("x")
    git("add", "-A")
    git("commit", "-qm", "docs")

    import os

    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        assert NG.subject("HEAD", ["packages/flow"]) == merge
        assert NG.subject("HEAD", ["packages/types"]) is None
        assert NG.subject(flow, ["packages/flow"]) == flow
    finally:
        os.chdir(cwd)


@pytest.mark.parametrize(
    "workflow, package",
    [
        ("release-program-graph.yml", "program-graph"),
        ("release-and-publish.yml", "launcher"),
    ],
)
def test_every_release_workflow_runs_the_rule_before_it_builds(
    workflow: str, package: str
) -> None:
    payload = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    for job in payload["jobs"].values():
        runs = [step.get("run") or "" for step in job.get("steps", [])]
        if any("newest_green.py" in run for run in runs):
            break
    else:
        pytest.fail(f"{workflow} never runs newest_green.py")
    assert job["permissions"]["actions"] == "read"
    gate = next(i for i, run in enumerate(runs) if "newest_green.py" in run)
    assert f"newest_green.py {package}" in runs[gate]
    # It runs before anything is built or published in that job.
    assert not any("build" in run and "python -m build" in run for run in runs[:gate])
    checkout = next(
        step for step in job["steps"] if "actions/checkout" in step.get("uses", "")
    )
    assert checkout["with"]["fetch-depth"] == 0


def test_every_released_package_has_a_rule() -> None:
    choices = yaml.safe_load((WORKFLOWS / "release-package.yml").read_text())[True][
        "workflow_dispatch"
    ]["inputs"]["package"]["options"]
    assert set(choices) - {"all"} <= set(NG.PACKAGES)
    assert {"desktop", "launcher", "program-graph"} <= set(NG.PACKAGES)
