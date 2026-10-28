"""release-package.yml publishes a merged version bump, under Desktop's rules.

scripts/ci/release_plan.py decides, per package, whether this run releases
it: the newest commit that changed it is green on main, the version is new
on PyPI and higher than every version there, no tag names it, and the
changelog starts with it. A version is never republished, and a half-published
one is resumed from PyPI's bytes on a dispatch, never rebuilt.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release-package.yml"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts" / "ci" / f"{name}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PLAN = _load("release_plan")
DIST = _load("pypi_dist")
FILES = [{"packagetype": "sdist"}, {"packagetype": "bdist_wheel"}]


def _decide(**overrides):
    state = {
        "version": "2.5.0",
        "changelog": "2.5.0",
        "pypi_versions": ["2.3.0", "2.4.0"],
        "pypi_files": None,
        "tag_commit": None,
        "release_published": False,
        "green": True,
        "resume": False,
    }
    return PLAN.decide(**{**state, **overrides})[0]


def test_a_new_green_version_with_its_changelog_releases() -> None:
    assert _decide() == "release"


@pytest.mark.parametrize(
    "overrides, action",
    [
        ({"green": False}, "wait"),
        ({"changelog": "2.4.0"}, "wait"),
        ({"changelog": None}, "wait"),
        # Never lower than, or equal to, what PyPI already has.
        ({"version": "2.3.9", "changelog": "2.3.9"}, "refuse"),
        ({"pypi_versions": ["2.5.0rc1", "2.6.0"]}, "refuse"),
        # A tag without the PyPI upload it follows.
        ({"tag_commit": "c" * 40}, "refuse"),
    ],
)
def test_it_publishes_nothing_it_may_not(overrides: dict, action: str) -> None:
    assert _decide(**overrides) == action


def test_a_released_version_is_done_and_never_republished() -> None:
    released = {"pypi_files": FILES, "tag_commit": "c" * 40, "release_published": True}
    assert _decide(**released) == "done"
    assert _decide(**released, resume=True) == "done"


def test_a_version_on_pypi_without_a_tag_is_done_not_rebuilt() -> None:
    # capture 1.3.0 predates package tags; a run that failed after its upload
    # is re-run, which reuses its bytes.
    assert _decide(pypi_files=FILES) == "done"
    assert _decide(pypi_files=FILES, resume=True) == "done"


def test_a_tagged_version_without_a_published_release_resumes_only_on_request() -> None:
    half = {"pypi_files": FILES, "tag_commit": "c" * 40, "release_published": False}
    assert _decide(**half) == "refuse"
    assert _decide(**half, resume=True) == "resume"
    # Resuming needs both files on PyPI.
    partial = {**half, "pypi_files": [{"packagetype": "bdist_wheel"}]}
    assert _decide(**partial, resume=True) == "refuse"


def test_versions_order_with_pre_releases_first() -> None:
    order = ["1.9.9", "1.10.0rc1", "1.10.0", "1.10.1", "2.0.0"]
    assert sorted(reversed(order), key=PLAN.version_key) == order


def test_the_changelog_top_entry_is_read_exactly() -> None:
    text = "# Changelog\n\n## v2.5.0 (2026-10-28)\n\n- x\n\n## v2.4.0 (2026-10-01)\n"
    assert PLAN.changelog_top(text) == "2.5.0"
    assert PLAN.changelog_top("## Unreleased\n") is None


def test_pypi_bytes_must_match_exactly() -> None:
    local = {"a.whl": "1", "a.tar.gz": "2"}
    assert DIST.compare(local, dict(local)) == []
    assert DIST.compare(local, {"a.whl": "1"}) == ["PyPI lacks a.tar.gz"]
    assert DIST.compare(local, {**local, "a.tar.gz": "3"}) == [
        "a.tar.gz differs from PyPI's"
    ]


def test_the_workflow_runs_on_main_after_packages_on_a_schedule_and_on_dispatch() -> (
    None
):
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = workflow[True]
    assert set(triggers) == {"workflow_run", "schedule", "workflow_dispatch"}
    assert triggers["workflow_run"]["workflows"] == ["Packages"]
    assert triggers["workflow_run"]["branches"] == ["main"]
    assert workflow["concurrency"]["cancel-in-progress"] is False
    jobs = workflow["jobs"]
    assert "release_plan.py" in jobs["plan"]["steps"][-1]["run"]
    assert jobs["plan"]["permissions"] == {"contents": "read", "actions": "read"}
    # Only the publish job may mint a PyPI token, in the package's environment.
    assert jobs["publish"]["environment"]["name"] == "pypi-${{ matrix.package }}"
    assert jobs["publish"]["permissions"] == {"id-token": "write"}
    for name, job in jobs.items():
        if name != "publish":
            assert "id-token" not in (job.get("permissions") or {}), name


def test_each_package_chain_gates_on_its_own_legs() -> None:
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    for name in ("build", "preflight-github", "publish", "publish-github"):
        assert jobs[name]["strategy"]["fail-fast"] is False, name

    def downloads(job: str) -> set[str]:
        return {
            step["with"]["name"]
            for step in jobs[job]["steps"]
            if "download-artifact" in step.get("uses", "")
        }

    assert "preflight-${{ matrix.package }}" in downloads("publish")
    assert "dist-${{ matrix.package }}" in downloads("publish")
    assert "preflight-${{ matrix.package }}" in downloads("publish-github")
    runs = "\n".join(step.get("run", "") for step in jobs["publish-github"]["steps"])
    # It tags only what PyPI serves, at the built (or already tagged) commit.
    assert "pypi_dist.py compare" in runs and "pypi_dist.py fetch" in runs
    assert "$GITHUB_SHA" not in runs and "$RELEASE_COMMIT" in runs
