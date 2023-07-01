"""openadapt.app.build module.

This module provides functionality for building the OpenAdapt application.

Example usage:
    python build.py
"""

import os
import subprocess
from pathlib import Path

import nicegui

spec = [
    "pyi-makespec",
    f"{Path(__file__).parent}/main.py",
    f"--icon={Path(__file__).parent}/assets/logo.ico",
    "--name",
    "OpenAdapt",  # name
    # "--onefile", # trade startup speed for smaller file size
    "--onedir",
    "--windowed",  # prevent console appearing, only use with ui.run(native=True, ...)
    "--add-data",
    f"{Path(nicegui.__file__).parent}{os.pathsep}nicegui",
]

subprocess.call(spec)

# add import sys ; sys.setrecursionlimit(sys.getrecursionlimit() * 5) to line 2 of OpenAdapt.spec
with open("OpenAdapt.spec", "r+") as f:
    lines = f.readlines()
    lines[1] = "import sys ; sys.setrecursionlimit(sys.getrecursionlimit() * 5)\n"
    f.seek(0)
    f.truncate()
    f.writelines(lines)

subprocess.call(["pyinstaller", "OpenAdapt.spec"])
