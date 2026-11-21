"""Every Flow module imports on its own, in a fresh interpreter.

``openadapt_flow/__init__.py`` loads its ``ir`` re-exports on first use, so a
program that starts by importing ``openadapt_flow.crypto`` loads a handful of
modules instead of the whole schema. Until that change, importing the package
always loaded ``ir`` first, and that order hid import cycles: with a lazy
package, ``import openadapt_flow.qualification`` as a first import used to fail
on a partially initialized module. A test run never sees that, because the
first test file to load imports ``ir``.

So each check here starts a new interpreter per module:

* the modules that had such a cycle run in every lane;
* every module in ``openadapt_flow`` runs outside the pull-request lane
  (``OPENADAPT_TEST_LANE=fast`` skips it; it takes a few CPU minutes).

A module that fails only because an optional third-party extra is missing is
skipped, not failed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

FLOW = Path(__file__).resolve().parents[1]

# Modules that failed to import first when the package was made lazy before
# the ir -> qualification -> execution_profiles cycle was broken.
CYCLE_PRONE = [
    "openadapt_flow.__main__",
    "openadapt_flow.action_evidence",
    "openadapt_flow.console.standard_changes",
    "openadapt_flow.execution_profiles",
    "openadapt_flow.qualification",
    "openadapt_flow.repair.self_repair",
    "openadapt_flow.repair.standard_change",
    "openadapt_flow.run_gate",
    "openadapt_flow.runner.hosted_adapter",
    "openadapt_flow.runner.protocol",
    "openadapt_flow.runtime.authorization",
    "openadapt_flow.runtime.durable.resume",
    "openadapt_flow.traversal",
    "openadapt_flow.visualize.builder",
]

_PROBE = """
import importlib, sys
name = sys.argv[1]
try:
    importlib.import_module(name)
except ModuleNotFoundError as error:
    missing = (error.name or "").split(".")[0]
    if missing and not missing.startswith("openadapt"):
        print("EXTRA", missing)
        sys.exit(0)
    raise
"""


def _all_modules() -> list[str]:
    names = []
    for path in sorted((FLOW / "openadapt_flow").rglob("*.py")):
        parts = path.relative_to(FLOW).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        names.append(".".join(parts))
    return names


def _import_alone(module: str) -> str | None:
    """None when ``module`` imports in a fresh interpreter; else the error."""
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, module],
        capture_output=True,
        text=True,
        cwd=FLOW,
        timeout=120,
    )
    if result.returncode == 0:
        return None
    lines = (result.stderr or result.stdout).strip().splitlines()
    return lines[-1] if lines else f"exit {result.returncode}"


def _failures(modules: list[str]) -> dict[str, str]:
    workers = max(1, min(4, os.cpu_count() or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = dict(zip(modules, pool.map(_import_alone, modules)))
    return {name: error for name, error in results.items() if error}


def test_cycle_prone_modules_import_alone() -> None:
    assert _failures(CYCLE_PRONE) == {}


@pytest.mark.skipif(
    os.environ.get("OPENADAPT_TEST_LANE") == "fast",
    reason="every module, one interpreter each: outside the pull-request lane",
)
def test_every_module_imports_alone() -> None:
    assert _failures(_all_modules()) == {}


def test_the_package_does_not_load_ir_eagerly() -> None:
    code = (
        "import sys, openadapt_flow.crypto\nprint('openadapt_flow.ir' in sys.modules)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "False"


def test_package_reexports_and_submodule_access_still_work() -> None:
    code = (
        "import openadapt_flow, openadapt_flow.ir as ir\n"
        "assert openadapt_flow.Workflow is ir.Workflow\n"
        "assert openadapt_flow.RunReport is ir.RunReport\n"
        "assert openadapt_flow.qualification.QualificationProject\n"
        "from openadapt_flow import ActionKind, Step\n"
        "assert ActionKind is ir.ActionKind and Step is ir.Step\n"
        "assert 'Workflow' in dir(openadapt_flow)\n"
        "try:\n"
        "    openadapt_flow.no_such_name\n"
        "except AttributeError:\n"
        "    pass\n"
        "else:\n"
        "    raise SystemExit('expected AttributeError')\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True, cwd=FLOW)
