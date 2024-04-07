"""openadapt.app.build module.

This module provides functionality for building the OpenAdapt application.

Example usage:
    python build.py
"""

from pathlib import Path
import os
import subprocess

import nicegui
import notifypy
import oa_pynput

additional_packages_to_install = [
    nicegui,
    notifypy,
    oa_pynput,
]

OPENADAPT_DIR = Path(__file__).parent

subprocess.call(
    ["npm", "run", "build"],
    cwd=OPENADAPT_DIR / "app" / "dashboard",
)

spec = [
    "pyi-makespec",
    f"{OPENADAPT_DIR / 'entrypoint.py'}",
    f"--icon={OPENADAPT_DIR / 'app' / 'assets' / 'logo.ico'}",
    "--name",
    "OpenAdapt",  # name
    # "--onefile", # trade startup speed for smaller file size
    "--onedir",
    "--windowed",  # prevent console appearing, only use with ui.run(native=True, ...)
]
ignore_dirs = [
    "__pycache__",
    ".vscode",
    ".git",
    ".idea",
    "performance",
    "node_modules",
    ".next",
    "dashboard/app",
    "dashboard/components",
    "dashboard/types",
    "performance",
]
ignore_file_extensions = [
    ".db",
    ".db-journal",
    ".md",
    "config.local.json",
    "openadapt/app/dashboard/.eslintrc.json",
    "openadapt/app/dashboard/.gitignore",
    "openadapt/app/dashboard/.nvmrc",
    "openadapt/app/dashboard/.prettierrc.json",
    "openadapt/app/dashboard/api.ts",
    "openadapt/app/dashboard/entrypoint.sh",
    "openadapt/app/dashboard/entrypoint.ps1",
    "openadapt/app/dashboard/index.js",
    "openadapt/app/dashboard/next.config.js",
    "openadapt/app/dashboard/package.json",
    "openadapt/app/dashboard/next-env.ts",
    "openadapt/app/dashboard/package-lock.json",
    "openadapt/app/dashboard/postcss.config.js",
    "openadapt/app/dashboard/tailwind.config.js",
    "openadapt/app/dashboard/tsconfig.json",
]

for root, dirs, files in os.walk(OPENADAPT_DIR):
    if any(ignore_dir in root for ignore_dir in ignore_dirs):
        continue
    for file in files:
        relative_path = Path(root).relative_to(OPENADAPT_DIR) / file
        file_relative_path = f'{Path("openadapt") / relative_path}'
        if any(file_relative_path.endswith(ext) for ext in ignore_file_extensions):
            continue
        spec.append("--add-data")
        file_parent_dir = Path(file_relative_path).parent
        spec.append(f"{Path(root) / file}:{file_parent_dir}")

for package in additional_packages_to_install:
    spec.append("--add-data")
    spec.append(f"{Path(package.__file__).parent}:{Path(package.__name__)}")

subprocess.call(spec)

# add import sys ; sys.setrecursionlimit(sys.getrecursionlimit() * 5)
#  to line 2 of OpenAdapt.spec
with open("OpenAdapt.spec", "r+") as f:
    lines = f.readlines()
    lines[1] = "import sys ; sys.setrecursionlimit(sys.getrecursionlimit() * 50)\n"
    f.seek(0)
    f.truncate()
    f.writelines(lines)

# building
proc = subprocess.Popen("pyinstaller OpenAdapt.spec --noconfirm", shell=True)
proc.wait()

# cleanup
os.remove("OpenAdapt.spec")
