"""Bundles and their sidecar files are UTF-8 whatever the machine's locale.

Python reads and writes text in the locale's encoding unless told otherwise,
which is not UTF-8 on Windows (or with ``LC_ALL=C``). A workflow named with
non-ASCII characters then saved and loaded differently on each machine.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from openadapt_flow.ir import ActionKind, Step, Workflow

NAME = "Überweisung – 患者 ✓"


def _workflow() -> Workflow:
    step = Step(id="s1", intent="Klick „Speichern“", action=ActionKind.KEY, key="Enter")
    return Workflow(name=NAME, steps=[step])


def test_non_ascii_bundle_round_trips(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    _workflow().save(bundle)
    loaded = Workflow.load(bundle)
    assert loaded.name == NAME
    assert loaded.steps[0].intent == "Klick „Speichern“"
    assert NAME in (bundle / "workflow.json").read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX C locale")
def test_bundle_round_trips_under_a_non_utf8_locale(tmp_path: Path) -> None:
    """Save and load in a child whose default text encoding is ASCII."""

    script = textwrap.dedent(
        f"""
        import locale, sys
        from pathlib import Path
        from openadapt_flow.ir import ActionKind, Step, Workflow

        assert locale.getpreferredencoding(False).lower() in (
            "ansi_x3.4-1968", "us-ascii", "ascii"
        ), locale.getpreferredencoding(False)
        name = {NAME!r}
        step = Step(id="s1", intent="Klick", action=ActionKind.KEY, key="Enter")
        bundle = Path(sys.argv[1])
        Workflow(name=name, steps=[step]).save(bundle)
        assert Workflow.load(bundle).name == name
        assert name in (bundle / "workflow.json").read_bytes().decode("utf-8")
        """
    )
    env = {
        **os.environ,
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONUTF8": "0",
        "PYTHONCOERCECLOCALE": "0",
    }
    done = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "bundle")],
        env=env,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
