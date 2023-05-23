import os
import subprocess
from pathlib import Path

import nicegui

cmd = [
    "python",
    "-m",
    "PyInstaller",
    "openadapt/app/main.py",  # main file with ui.run()
    "--icon=assets/logo.ico",
    "--name",
    "OpenAdapt",  # name
    # "--onefile", # trade startup speed for smaller file size
    "--onedir",
    "--windowed",  # prevent console appearing, only use with ui.run(native=True, ...)
    "--add-data",
    f"{Path(nicegui.__file__).parent}{os.pathsep}nicegui",
]
subprocess.call(cmd)
