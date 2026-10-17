"""Every command the public docs tell a partner to run parses in this code.

B072: the README and agents pages once showed `serve --allow-run` and the
`[tutorial]` extra while PyPI shipped 2.0.1, which had neither, so a
partner's first command exited with code 2. These checks keep the documented
commands and extras in line with the code and package they ship with. The
published-release half (does the latest PyPI version accept them?) is a
scheduled docs check outside this repository.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

from openadapt_agent.cli import build_parser

REPO_ROOT = Path(__file__).resolve().parents[1]
#: Surfaces that tell a reader what to run. Design notes that quote a
#: rejected command on purpose are not listed.
SURFACES = (
    REPO_ROOT / "README.md",
    REPO_ROOT .parents[1] / "llms.txt",
    REPO_ROOT / "skills" / "openadapt-gui-write" / "SKILL.md",
)
_INVOCATION = re.compile(r"openadapt-agent ((?:serve|authoring|emit-skill)\b[^`|\n]*)")
_EXTRA = re.compile(r"openadapt-agent\[([^\]]+)\]")
_PLACEHOLDER = re.compile(r"<[^>]*>")


def _documented_invocations() -> list[tuple[str, str]]:
    found = []
    for surface in SURFACES:
        for line in surface.read_text(encoding="utf-8").splitlines():
            for match in _INVOCATION.finditer(line):
                command = _PLACEHOLDER.sub("PLACEHOLDER", match.group(1)).strip()
                found.append((surface.name, command.rstrip(".,:;)")))
    return found


def _declared_extras() -> set[str]:
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        block = text.split("[project.optional-dependencies]", 1)[1].split("\n[", 1)[0]
        return set(re.findall(r"(?m)^([A-Za-z0-9_-]+)\s*=", block))
    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        return set(tomllib.load(fh)["project"]["optional-dependencies"])


INVOCATIONS = _documented_invocations()


def test_docs_document_the_serve_commands():
    commands = {command for _, command in INVOCATIONS}
    assert "serve" in commands
    assert any(command.startswith("serve --authoring") for command in commands)


@pytest.mark.parametrize("surface,command", INVOCATIONS)
def test_documented_command_parses(surface, command):
    argv = shlex.split(command)
    try:
        build_parser().parse_args(argv)
    except SystemExit as exc:
        # --help exits 0; a usage error exits 2 and fails this test.
        assert exc.code == 0, f"{surface}: `openadapt-agent {command}` is rejected"


def test_documented_extras_exist():
    declared = _declared_extras()
    for surface in SURFACES:
        for extras in _EXTRA.findall(surface.read_text(encoding="utf-8")):
            for extra in extras.split(","):
                assert extra.strip() in declared, (surface.name, extra)
