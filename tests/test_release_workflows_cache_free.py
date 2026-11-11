"""Release workflows restore no dependency or build cache.

A cache written by an earlier run could put unreviewed bytes into a published
wheel or a signed installer. Pull-request, merge-queue and main builds may
cache; the release workflows install from lockfiles on a clean runner.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

GITHUB = Path(__file__).resolve().parents[1] / ".github"
RELEASES = [
    "workflows/desktop-release.yml",
    "workflows/release-package.yml",
    "workflows/release-and-publish.yml",
    "workflows/signpath-test-sign.yml",
    # The Windows build that desktop-release.yml and signpath-test-sign.yml share.
    "actions/build-desktop-windows-installers/action.yml",
]

# Local actions that can cache but take a `use-cache` input. A release passes
# `use-cache: "false"`, and the action gates every cache step on that input.
CACHE_OPTIONAL = {"actions/apt-install"}


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
    text = (GITHUB / name).read_text(encoding="utf-8")
    for uses, block in _steps(text):
        assert not re.match(
            r"(actions/cache(/restore|/save)?|Swatinem/rust-cache)@", uses
        ), (
            name,
            uses,
        )
        assert not re.search(r"^\s+cache: ", block, re.MULTILINE), (name, uses, block)
        if uses.startswith("actions/setup-node@"):
            assert re.search(r"package-manager-cache: false", block), (name, uses)
        if uses.startswith("astral-sh/setup-uv@"):
            assert re.search(r"enable-cache: false", block), (name, uses)


@pytest.mark.parametrize("name", RELEASES)
def test_local_actions_a_release_uses_are_checked_too(name: str) -> None:
    text = (GITHUB / name).read_text(encoding="utf-8")
    for uses, _ in _steps(text):
        if uses.startswith("./.github/"):
            local = uses.removeprefix("./.github/")
            if local in CACHE_OPTIONAL:
                continue
            assert f"{local}/action.yml" in RELEASES, (name, uses)


@pytest.mark.parametrize("name", RELEASES)
def test_releases_turn_off_the_cache_of_cache_optional_actions(name: str) -> None:
    text = (GITHUB / name).read_text(encoding="utf-8")
    for uses, block in _steps(text):
        if uses.removeprefix("./.github/") in CACHE_OPTIONAL:
            assert re.search(r'^\s+use-cache: "false"$', block, re.MULTILINE), (
                name,
                block,
            )


@pytest.mark.parametrize("action", sorted(CACHE_OPTIONAL))
def test_cache_optional_actions_gate_every_cache_step(action: str) -> None:
    text = (GITHUB / action / "action.yml").read_text(encoding="utf-8")
    cache_steps = [
        block
        for uses, block in _steps(text)
        if re.match(r"(actions/cache(/restore|/save)?|Swatinem/rust-cache)@", uses)
    ]
    assert cache_steps, action
    # _steps returns the lines after `uses:`; the `if:` sits above it, so read
    # each step from its `- name:` line instead.
    for step in re.split(r"\n    - ", text.split("\n  steps:\n", 1)[1]):
        if re.search(r"uses: (actions/cache|Swatinem/rust-cache)", step):
            assert re.search(r"if: inputs\.use-cache == 'true'", step), step
