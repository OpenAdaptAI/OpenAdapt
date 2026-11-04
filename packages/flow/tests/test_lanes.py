"""Every entry in tests/nightly_only.txt names a test that exists."""

from __future__ import annotations

import ast
from pathlib import Path

from tests import lanes

ROOT = Path(__file__).resolve().parent.parent


def test_every_listed_test_exists() -> None:
    entries = lanes.nightly_only()
    assert entries
    for entry in entries:
        file, _, name = entry.partition("::")
        path = ROOT / file
        assert path.is_file(), entry
        if name:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = {
                node.name
                for node in ast.walk(tree)
                if isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                )
            }
            assert name.split("::")[0].split("[")[0] in names, entry
    assert len(entries) == len(set(entries))


def test_listed_ids_match_files_tests_and_cases() -> None:
    entries = ["tests/a.py", "tests/b.py::test_x"]
    assert lanes._listed("tests/a.py::test_any", entries)
    assert lanes._listed("tests/b.py::test_x", entries)
    assert lanes._listed("tests/b.py::test_x[case-1]", entries)
    assert not lanes._listed("tests/b.py::test_xy", entries)
    assert not lanes._listed("tests/ab.py::test_any", entries)
