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
import subprocess
import sys
import textwrap
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


# Runtime complement to the static check above: import the package fresh in a
# subprocess where the display is simulated as unavailable (mss.grab raises
# ScreenShotError and utils.take_screenshot raises), and assert the import and
# the high-level load API still succeed. This reproduces a genuinely headless
# host and fails if any import-time code path touches the screen.
_HEADLESS_IMPORT_SCRIPT = textwrap.dedent(
    """
    import mss
    import mss.exception

    class _HeadlessSct:
        # monitors[0] reports a zero-size region, as headless hosts do.
        monitors = [{"left": 0, "top": 0, "width": 0, "height": 0}]

        def grab(self, monitor):
            raise mss.exception.ScreenShotError("headless: no display")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    # Any attempt to screenshot at import time now fails.
    mss.mss = lambda *a, **k: _HeadlessSct()

    import openadapt_capture
    from openadapt_capture import Capture, CaptureSession

    # take_screenshot must also raise, proving nothing depends on it at import.
    from openadapt_capture import utils

    def _boom(*a, **k):
        raise mss.exception.ScreenShotError("headless: no display")

    utils.take_screenshot = _boom

    assert CaptureSession is not None
    assert Capture is not None
    print("HEADLESS_IMPORT_OK")
    """
)


def test_package_imports_when_screenshot_fails():
    """`import openadapt_capture` must not crash on a headless host.

    Simulates a host with no usable display (mss.grab raises
    ScreenShotError) and imports the package + high-level API fresh in a
    subprocess.
    """
    result = subprocess.run(
        [sys.executable, "-c", _HEADLESS_IMPORT_SCRIPT],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "Importing openadapt_capture crashed on a simulated headless host.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "HEADLESS_IMPORT_OK" in result.stdout
