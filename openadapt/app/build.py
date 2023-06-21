import os
import subprocess
from pathlib import Path

import nicegui

spec = [
    "pyi-makespec",
    f"{Path(__file__).parent}/tray.py",
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

# stop pyinstaller from running the tray
f = open(f"{Path(__file__).parent}/__init__.py", "r+")
init_file = f.readlines()
bak = init_file.copy()
f.seek(0)
f.truncate()

# building
proc = subprocess.Popen("pyinstaller OpenAdapt.spec", shell=True)
proc.wait()

# restore __init__.py
f.writelines(bak)
f.close()
