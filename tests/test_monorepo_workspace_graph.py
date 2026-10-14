"""The monorepo CI plan must test every package a change can affect."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "monorepo"))

import workspace_graph as graph  # noqa: E402


def test_every_workspace_package_has_a_ci_lane() -> None:
    packages = graph.load_packages()
    assert set(packages) == set(graph.LANES)


def test_a_change_runs_the_package_and_everything_that_depends_on_it() -> None:
    packages = graph.load_packages()
    changed = ["packages/types/openadapt_types/__init__.py"]
    selected = graph.affected(changed, packages)
    assert "openadapt-types" in selected
    for name, info in packages.items():
        if "openadapt-types" in info["depends_on"]:
            assert name in selected


def test_dependents_are_followed_transitively() -> None:
    packages = graph.load_packages()
    selected = graph.affected(["packages/privacy/openadapt_privacy/scan.py"], packages)
    assert {"openadapt-privacy", "openadapt-capture", "openadapt"} <= set(selected)


def test_capture_recordings_also_run_the_viewer() -> None:
    packages = graph.load_packages()
    changed = ["packages/capture/examples/captures/demo_new/recording.db"]
    assert "openadapt-viewer" in graph.affected(changed, packages)


def test_lockfile_change_runs_every_package() -> None:
    packages = graph.load_packages()
    assert graph.affected(["uv.lock"], packages) == sorted(packages)


def test_docs_only_change_runs_nothing() -> None:
    packages = graph.load_packages()
    assert graph.affected(["docs/index.md"], packages) == []
