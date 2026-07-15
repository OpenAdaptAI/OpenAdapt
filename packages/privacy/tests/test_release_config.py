"""Regression checks for the protected-main semantic release path."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_zero_major_patch_does_not_become_one_dot_zero() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r"(?m)^major_on_zero\s*=\s*false\s*$", pyproject)


def test_release_uses_protected_branch_credential_everywhere() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "token: ${{ secrets.ADMIN_TOKEN }}" in workflow
    assert workflow.count("github_token: ${{ secrets.ADMIN_TOKEN }}") == 2
    assert "secrets.GITHUB_TOKEN" not in workflow
