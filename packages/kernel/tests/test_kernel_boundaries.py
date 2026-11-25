"""Boundary tests: zero openadapt_flow imports, zero app-specific names, and strict <3,200 LOC budget."""

from __future__ import annotations

import ast
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1] / "src" / "openadapt_kernel"
FORBIDDEN_IMPORTS = ("openadapt_flow", "openadapt_desktop", "openadapt_capture")
FORBIDDEN_APP_NAMES = ("openemr", "medplum", "openimis", "mealie", "vikunja", "nationwide", "dentrix")


def test_zero_legacy_engine_imports_and_zero_app_names() -> None:
    py_files = sorted(SRC_DIR.rglob("*.py"))
    assert py_files
    total_lines = 0
    for path in py_files:
        text = path.read_text(encoding="utf-8")
        total_lines += len(text.splitlines())
        tree = ast.parse(text, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for bad in FORBIDDEN_IMPORTS:
                        assert not alias.name.startswith(bad), f"{path} imports {alias.name}"
            elif isinstance(node, ast.ImportFrom) and node.module:
                for bad in FORBIDDEN_IMPORTS:
                    assert not node.module.startswith(bad), f"{path} imports from {node.module}"
        lower = text.lower()
        for app in FORBIDDEN_APP_NAMES:
            assert app not in lower, f"{path} contains app-specific string {app!r}"

    assert total_lines < 3200, f"Kernel exceeded 3,200 LOC budget: {total_lines} lines"
