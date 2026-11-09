"""openadapt-types is the leaf package: pydantic only, no Flow.

Installing the shared models must never pull openadapt-flow and its vision
stack. These tests pin the declared dependency list and prove the package and
every submodule import with openadapt_flow blocked. The install-weight CI job
checks the same thing against a real wheel install.
"""

from __future__ import annotations

import ast
import pkgutil
import re
import subprocess
import sys
from pathlib import Path

import openadapt_types

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_the_only_runtime_dependency_is_pydantic() -> None:
    match = re.search(r"(?ms)^dependencies = \[\n(?P<body>.*?)^\]\n", PYPROJECT)
    assert match is not None
    assert ast.literal_eval("[" + match.group("body") + "]") == ["pydantic>=2.5"]
    requires_python = re.search(r'(?m)^requires-python = "([^"]+)"', PYPROJECT)
    assert requires_python is not None and "<3.13" not in requires_python.group(1)


def test_every_submodule_imports_without_flow() -> None:
    modules = [
        name
        for _, name, _ in pkgutil.walk_packages(openadapt_types.__path__, "openadapt_types.")
        if not name.endswith("__main__")
    ]
    assert "openadapt_types.check_level" in modules
    blocked = ("openadapt_flow", "cv2", "numpy", "onnxruntime", "rapidocr_onnxruntime")
    script = f"""
import importlib, importlib.abc, sys

class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {blocked!r}:
            raise ModuleNotFoundError(f"blocked {{fullname}}", name=fullname)
        return None

sys.meta_path.insert(0, Block())
for name in {modules!r}:
    importlib.import_module(name)
print("LEAF_OK")
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "LEAF_OK" in result.stdout


def test_vocab_lint_runs_as_a_module() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "openadapt_types.vocabulary", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
