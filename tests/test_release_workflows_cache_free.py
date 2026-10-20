"""Release workflows restore no dependency or build cache.

A cache written by an earlier run could put unreviewed bytes into a published
wheel or a signed installer. Pull-request, merge-queue and main builds may
cache; the release workflows install from lockfiles on a clean runner.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
RELEASES = ["desktop-release.yml", "release-package.yml", "release-and-publish.yml"]


def _steps(text: str) -> list[tuple[str, str]]:
    """(uses, its indented block) for every step that uses an action."""
    lines = text.splitlines()
    found = []
    for i, line in enumerate(lines):
        match = re.match(r"^(\s*)(?:- )?uses: (\S+)", line)
        if not match:
            continue
        indent = len(match.group(1)) + (2 if line.lstrip().startswith("- ") else 0)
        block = []
        for following in lines[i + 1 :]:
            if following.strip() and len(following) - len(following.lstrip()) < indent:
                break
            if re.match(rf"^\s{{{indent}}}- ", following):
                break
            block.append(following)
        found.append((match.group(2), "\n".join(block)))
    return found


@pytest.mark.parametrize("name", RELEASES)
def test_release_workflow_restores_no_cache(name: str) -> None:
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    for uses, block in _steps(text):
        assert not re.match(r"(actions/cache|Swatinem/rust-cache)@", uses), (name, uses)
        assert not re.search(r"^\s+cache: ", block, re.MULTILINE), (name, uses, block)
        if uses.startswith("actions/setup-node@"):
            assert re.search(r"package-manager-cache: false", block), (name, uses)
        if uses.startswith("astral-sh/setup-uv@"):
            assert re.search(r"enable-cache: false", block), (name, uses)
