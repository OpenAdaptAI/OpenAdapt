import os
import subprocess
from pathlib import Path
import nicegui

cmd = [
    "python",
    "-m",
    "PyInstaller",
    "main.py",  # your main file with ui.run()
    "--icon=assets/logo.ico",
    "--name",
    "OpenAdapt",  # name of your app
    "--onefile",
    "--windowed",  # prevent console appearing, only use with ui.run(native=True, ...)
    "--add-data",
    f"{Path(nicegui.__file__).parent}{os.pathsep}nicegui",
]
subprocess.call(cmd)
