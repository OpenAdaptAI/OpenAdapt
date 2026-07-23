"""Contracts for the installed package's default runtime API."""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _runtime_dependency_names() -> set[str]:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(
        r"(?ms)^dependencies = \[\n(?P<requirements>.*?)^\]\n",
        pyproject,
    )
    assert match is not None
    requirements = ast.literal_eval("[" + match.group("requirements") + "]")
    return {
        re.split(r"[\s\[<>=!~;]", requirement, maxsplit=1)[0].lower()
        for requirement in requirements
    }


def test_recorder_import_dependencies_are_in_the_default_install() -> None:
    """The default package API must not rely on development-only packages."""
    assert "matplotlib" in _runtime_dependency_names()


def test_macos_accessibility_uses_permissive_runtime_dependencies() -> None:
    """The MIT package must not pull a GPL accessibility wrapper."""
    runtime_dependencies = _runtime_dependency_names()
    assert "oa-atomacos" not in runtime_dependencies
    assert "pyobjc-framework-applicationservices" in runtime_dependencies

    package_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "openadapt_capture").rglob("*.py")
    )
    assert "oa_atomacos" not in package_sources


def test_native_input_observers_do_not_ship_pynput() -> None:
    """The default runtime and packaged source stay outside pynput's LGPL path."""
    assert "pynput" not in _runtime_dependency_names()
    package_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "openadapt_capture").rglob("*.py")
    )
    assert "pynput" not in package_sources


def test_default_install_exposes_recorder() -> None:
    """Recorder import never fails because a runtime dependency is undeclared."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import importlib; "
                "\ntry:\n"
                "    module = importlib.import_module('openadapt_capture.recorder')\n"
                "except ImportError as exc:\n"
                "    assert exc.name != 'matplotlib' and "
                "'matplotlib' not in str(exc), exc\n"
                "else:\n"
                "    assert module.Recorder is not None\n"
            ),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "Recorder depended on an undeclared default-runtime package.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
