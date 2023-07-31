"""openadapt.app.build module.

This module provides functionality for building the OpenAdapt application.

Example usage:
    python build.py
"""

from pathlib import Path
import os
import subprocess

import nicegui

s = os.path.sep

spec = [
    "pyi-makespec",
    f"{Path(__file__).parent}{s}tray.py",
    f"--icon={Path(__file__).parent}{s}assets{s}logo.ico",
    "--name",
    "OpenAdapt",  # name
    # "--onefile", # trade startup speed for smaller file size
    "--onedir",
    "--windowed",  # prevent console appearing, only use with ui.run(native=True, ...)
    "--add-data",
    f"{Path(nicegui.__file__).parent}{s}nicegui",
    "--add-data",
    f"{Path(__file__).parent}{s}assets",
]

subprocess.call(spec)

# add import sys ; sys.setrecursionlimit(sys.getrecursionlimit() * 5)
#  to line 2 of OpenAdapt.spec
with open("OpenAdapt.spec", "r+") as f:
    lines = f.readlines()
    lines[1] = "import sys ; sys.setrecursionlimit(sys.getrecursionlimit() * 5)\n"
    f.seek(0)
    f.truncate()
    f.writelines(lines)

# building
proc = subprocess.Popen("pyinstaller OpenAdapt.spec", shell=True)
proc.wait()
