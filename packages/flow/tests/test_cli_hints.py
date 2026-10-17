"""Next-step hints use the spelling the person typed."""

from __future__ import annotations

import pytest

from openadapt_flow.cli_hints import (
    ENGINE_PREFIX,
    LAUNCHER_PREFIX,
    command,
    command_prefix,
)


@pytest.mark.parametrize(
    "argv0",
    [
        "/usr/local/bin/openadapt",
        "openadapt",
        r"C:\Users\me\venv\Scripts\openadapt.exe",
        "OPENADAPT.EXE",
    ],
)
def test_launcher_spelling(argv0: str) -> None:
    assert command_prefix([argv0, "flow", "demo"]) == LAUNCHER_PREFIX
    assert command("demo", [argv0]) == "openadapt flow demo"


@pytest.mark.parametrize(
    "argv0",
    [
        "/usr/local/bin/openadapt-flow",
        "openadapt-flow.exe",
        "/x/openadapt_flow/__main__.py",
        "-m",
        "pytest",
        "",
    ],
)
def test_engine_spelling(argv0: str) -> None:
    assert command_prefix([argv0]) == ENGINE_PREFIX
    assert command("explain run", [argv0]) == "openadapt-flow explain run"


def test_empty_argv_keeps_engine_spelling() -> None:
    assert command_prefix([]) == ENGINE_PREFIX


def _engine_verbs() -> set[str]:
    import argparse

    from openadapt_flow.__main__ import build_parser

    verbs: set[str] = set()
    for action in build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            verbs |= set(action.choices)
    return verbs


def test_printed_hints_never_hard_code_the_engine_spelling() -> None:
    """A printed next step names a verb through ``command()``.

    Under the launcher (``openadapt flow ...``) a literal
    ``openadapt-flow <verb>`` names a program the person never typed. This
    scans every ``print(...)`` call in the package for a string literal that
    spells a registered verb with the engine prefix.
    """
    import ast
    import re
    from pathlib import Path

    import openadapt_flow

    verbs = _engine_verbs()
    pattern = re.compile(r"openadapt-flow ([a-z][a-z-]*)")
    package = Path(openadapt_flow.__file__).parent
    offenders: list[str] = []
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
            ):
                continue
            for part in ast.walk(node):
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    for match in pattern.finditer(part.value):
                        if match.group(1) in verbs:
                            offenders.append(
                                f"{path.relative_to(package)}:{part.lineno}: "
                                f"{match.group(0)}"
                            )
    assert offenders == []
