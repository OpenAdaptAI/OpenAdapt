"""The "Manual Demo Lifecycle" in `openadapt --help` runs start to finish.

The help listed `openadapt flow lint bundle --strict` as step 3. Strict lint
exits 1 on the demo bundle by design, because the demo's irreversible save has
warn-level gaps (no effect contract, same-channel check). A script that chained
the documented steps with `&&` or `set -e` stopped there. Plain `lint` prints
the same warnings and exits 0.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys

import pytest
from click.testing import CliRunner

from openadapt.cli import main as cli_main


def _lifecycle_commands() -> list[list[str]]:
    """Return the help's Manual Demo Lifecycle commands as argv lists."""
    result = CliRunner().invoke(cli_main, ["--help"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    start = next(i for i, line in enumerate(lines) if "Manual Demo Lifecycle" in line)
    commands = []
    for line in lines[start + 1 :]:
        if not line.strip().startswith("openadapt "):
            break
        commands.append(shlex.split(line))
    assert commands, result.output
    return commands


def test_lifecycle_lists_no_step_that_fails_on_the_demo_bundle():
    commands = _lifecycle_commands()

    assert [argv[2] for argv in commands] == [
        "demo-record",
        "compile",
        "lint",
        "certify",
        "replay",
    ]
    for argv in commands:
        assert "--strict" not in argv, argv


@pytest.mark.skipif(
    not os.environ.get("OPENADAPT_E2E"),
    reason="runs a browser; set OPENADAPT_E2E=1 to run",
)
def test_lifecycle_runs_end_to_end(tmp_path):
    pytest.importorskip("openadapt_flow", reason="openadapt-flow not installed")
    launcher = [sys.executable, "-c", "from openadapt.cli import main; main()"]
    for argv in _lifecycle_commands():
        completed = subprocess.run(
            [*launcher, *argv[1:]],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert completed.returncode == 0, (argv, completed.stdout, completed.stderr)
