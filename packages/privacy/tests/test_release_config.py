"""Regression checks for the monorepo release path.

openadapt-privacy releases from the OpenAdapt monorepo through the root
``Release package`` workflow, which refuses a version whose changelog section is
missing. These checks catch that before a release is dispatched.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RELEASE_WORKFLOW = ROOT.parents[1] / ".github/workflows/release-package.yml"


def test_changelog_starts_with_the_project_version() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    version = re.search(r'(?m)^version = "([^"]+)"$', pyproject).group(1)
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    headers = re.findall(r"^## v([^ ]+) \(\d{4}-\d{2}-\d{2}\)$", changelog, re.MULTILINE)
    assert headers, "CHANGELOG.md has no release sections"
    assert headers[0] == version


def test_monorepo_release_workflow_publishes_this_package() -> None:
    if not RELEASE_WORKFLOW.is_file():
        pytest.skip("not running from the OpenAdapt monorepo checkout")
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    # The workflow's plan job decides releases from this package map.
    plan = (RELEASE_WORKFLOW.parents[2] / "scripts/ci/release_plan.py").read_text(encoding="utf-8")
    assert '"privacy": "openadapt-privacy"' in plan
    assert "release_plan.py" in workflow
    assert re.search(r"options: \[[^\]]*\bprivacy\b", workflow)
