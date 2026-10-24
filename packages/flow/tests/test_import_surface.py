"""Names other code relies on stay importable while large modules are split.

The decomposition plan (docs/design/DECOMPOSITION.md) moves code out of the
modules below. Each move re-exports what it moves, so every import that works
today keeps working. This test pins that surface: the names each module
defines at top level, the names any file in the monorepo imports from it, and
the methods of ``Replayer`` (tests call private methods on an instance).

The snapshot is ``fixtures/import_surface.json``. When a change deliberately
removes a name (dead code, not a move), regenerate it and say why in the pull
request:

    OPENADAPT_WRITE_IMPORT_SURFACE=1 pytest tests/test_import_surface.py
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import re
from pathlib import Path

import pytest

FLOW = Path(__file__).resolve().parents[1]
PACKAGES = FLOW.parent
SNAPSHOT = Path(__file__).parent / "fixtures" / "import_surface.json"
MODULES = {
    "openadapt_flow.runtime.replayer": "openadapt_flow/runtime/replayer.py",
    "openadapt_flow.__main__": "openadapt_flow/__main__.py",
    "openadapt_flow.qualification": "openadapt_flow/qualification.py",
    "openadapt_flow.ir": "openadapt_flow/ir.py",
}
CLASSES = {"openadapt_flow.runtime.replayer": ["Replayer"]}


def _defined(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _imported_elsewhere(module: str) -> set[str]:
    pattern = re.compile(
        rf"^\s*from {re.escape(module)} import (\([^)]*\)|[^\n]*)", re.MULTILINE
    )
    names: set[str] = set()
    for path in PACKAGES.rglob("*.py"):
        if ".venv" in path.parts or "node_modules" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in pattern.finditer(text):
            clause = re.sub(r"#[^\n]*", "", match.group(1))
            for item in clause.strip("()").split(","):
                name = item.strip().split(" as ")[0].strip()
                if name.isidentifier():
                    names.add(name)
    return names


def _methods(module: str, cls: str) -> set[str]:
    source = (FLOW / MODULES[module]).read_text(encoding="utf-8")
    for node in ast.parse(source).body:
        if isinstance(node, ast.ClassDef) and node.name == cls:
            return {
                n.name
                for n in node.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
    raise AssertionError(f"{module}.{cls} not found")


def _current() -> dict[str, list[str]]:
    surface: dict[str, list[str]] = {}
    for module, rel in MODULES.items():
        source = (FLOW / rel).read_text(encoding="utf-8")
        surface[module] = sorted(_defined(source) | _imported_elsewhere(module))
        for cls in CLASSES.get(module, []):
            surface[f"{module}:{cls}"] = sorted(_methods(module, cls))
    return surface


def test_snapshot_is_current_when_regenerating() -> None:
    if os.environ.get("OPENADAPT_WRITE_IMPORT_SURFACE") != "1":
        pytest.skip("set OPENADAPT_WRITE_IMPORT_SURFACE=1 to regenerate")
    SNAPSHOT.write_text(json.dumps(_current(), indent=1) + "\n", encoding="utf-8")


@pytest.mark.parametrize("module", sorted(MODULES))
def test_every_pinned_name_is_still_importable(module: str) -> None:
    pinned = json.loads(SNAPSHOT.read_text(encoding="utf-8"))[module]
    imported = importlib.import_module(module)
    missing = [name for name in pinned if not hasattr(imported, name)]
    assert missing == [], (
        f"{module} no longer provides {missing}. A move must re-export what it "
        "moves; see the docstring of this test to regenerate after a deliberate "
        "removal."
    )


@pytest.mark.parametrize(
    "module, cls", [(m, c) for m, classes in sorted(CLASSES.items()) for c in classes]
)
def test_every_pinned_method_is_still_on_the_class(module: str, cls: str) -> None:
    pinned = json.loads(SNAPSHOT.read_text(encoding="utf-8"))[f"{module}:{cls}"]
    klass = getattr(importlib.import_module(module), cls)
    missing = [name for name in pinned if not hasattr(klass, name)]
    assert missing == [], f"{module}.{cls} lost methods {missing}"
