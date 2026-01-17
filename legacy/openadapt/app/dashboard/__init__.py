"""Script to install dependencies needed for the dashboard."""

import os
import subprocess
import sys


def _run(bash_script: str) -> int:
    return subprocess.call(bash_script, shell=True)


def entrypoint() -> None:
    """Entrypoint for the installation script."""
    cwd = os.path.dirname(os.path.realpath(__file__))
    os.chdir(cwd)

    if sys.platform == "win32":
        _run("powershell -File entrypoint.ps1")
        return
    _run("source ./entrypoint.sh")
