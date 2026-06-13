"""Importing openadapt_capture must not call display APIs at module scope.

recorder.py used to compute monitor dimensions via a screenshot at
module scope (`monitor_width, monitor_height = utils.take_screenshot()
.size`), so `import openadapt_capture` crashed in any headless
environment whose display reported a zero-size region, taking down
`openadapt version`/`doctor` with it.

A subprocess import test is unreliable here (it only reproduces on a
genuinely headless display), so this guards the invariant statically:
no module-level call to a display/screenshot API in any package module.
Importing a library should be cheap and side-effect free.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "openadapt_capture"

# Calls that touch the screen/display and must not run at import time.
FORBIDDEN_AT_MODULE_SCOPE = {"take_screenshot", "get_monitor_dims", "grab"}


def _module_level_calls(tree: ast.Module):
    """Yield Call nodes that execute at import (module body, not inside
    a function or class definition)."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                yield sub


def _called_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def test_no_display_calls_at_module_scope():
    problems = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for call in _module_level_calls(tree):
            name = _called_name(call)
            if name in FORBIDDEN_AT_MODULE_SCOPE:
                problems.append(f"{path}:{call.lineno}: module-level {name}()")
    assert not problems, (
        "Display/screenshot calls at import time break headless imports "
        "(CI, servers, containers). Move them inside the function that "
        "uses them:\n  " + "\n  ".join(problems)
    )
