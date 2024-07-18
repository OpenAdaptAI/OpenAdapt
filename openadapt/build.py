"""openadapt.app.build module.

This module provides functionality for building the OpenAdapt application.

Example usage:
    python build.py
"""

from pathlib import Path
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile

import gradio_client
import nicegui
import oa_pynput
import pycocotools
import pydicom
import pyqttoast
import spacy_alignments
import ultralytics
import whisper

from openadapt.config import POSTHOG_HOST, POSTHOG_PUBLIC_KEY

if sys.platform == "win32":
    import screen_recorder_sdk


def build_pyinstaller() -> None:
    """Build the application using PyInstaller."""
    additional_packages_to_install = [
        nicegui,
        oa_pynput,
        pydicom,
        spacy_alignments,
        gradio_client,
        ultralytics,
        pycocotools,
        pyqttoast,
        whisper,
    ]
    if sys.platform == "win32":
        additional_packages_to_install.append(screen_recorder_sdk)
    packages_to_exclude = [
        "pytest",
        "py",
    ]

    packages_metadata_to_copy = [
        "replicate",
    ]

    OPENADAPT_DIR = Path(__file__).parent
    ROOT_DIR = OPENADAPT_DIR.parent

    if sys.platform == "win32":
        subprocess.run(
            ["npm", "run", "build"],
            cwd=OPENADAPT_DIR / "app" / "dashboard",
            shell=True,
            env={
                **os.environ,
                "NEXT_PUBLIC_POSTHOG_HOST": POSTHOG_HOST,
                "NEXT_PUBLIC_POSTHOG_PUBLIC_KEY": POSTHOG_PUBLIC_KEY,
            },
        )
    else:
        subprocess.run(
            "npm run build",
            cwd=OPENADAPT_DIR / "app" / "dashboard",
            shell=True,
            env={
                **os.environ,
                "NEXT_PUBLIC_POSTHOG_HOST": POSTHOG_HOST,
                "NEXT_PUBLIC_POSTHOG_PUBLIC_KEY": POSTHOG_PUBLIC_KEY,
            },
        )

    spec = [
        "pyi-makespec",
        f"{OPENADAPT_DIR / 'entrypoint.py'}",
        f"--icon={OPENADAPT_DIR / 'app' / 'assets' / 'logo.ico'}",
        "--name",
        "OpenAdapt",  # name
        # "--onefile", # trade startup speed for smaller file size
        "--onedir",
        # prevent console appearing, only use with ui.run(native=True, ...)
        "--windowed",
        "--hidden-import=tiktoken_ext.openai_public",
        "--hidden-import=tiktoken_ext",
        "--hidden-import=replicate",
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
        ".log",
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
            if any(
                file_relative_path.endswith(str(Path(ext)))
                for ext in ignore_file_extensions
            ):
                continue
            spec.append("--add-data")
            file_parent_dir = Path(file_relative_path).parent
            spec.append(f"{Path(root) / file}:{file_parent_dir}")

    for package in additional_packages_to_install:
        spec.append("--add-data")
        spec.append(f"{Path(package.__file__).parent}:{Path(package.__name__)}")

    for package in packages_to_exclude:
        spec.append("--exclude-module")
        spec.append(package)

    for package in packages_metadata_to_copy:
        spec.append("--copy-metadata")
        spec.append(package)

    subprocess.call(spec)

    # building
    proc = subprocess.Popen("pyinstaller OpenAdapt.spec --noconfirm", shell=True)
    proc.wait()

    # cleanup
    os.remove("OpenAdapt.spec")

    if sys.platform == "darwin":
        # because the app needs a stdout and stderr, we use a shell script to run the
        # app on new terminal
        shutil.move(
            ROOT_DIR / "dist" / "OpenAdapt.app" / "Contents" / "MacOS" / "OpenAdapt",
            ROOT_DIR
            / "dist"
            / "OpenAdapt.app"
            / "Contents"
            / "MacOS"
            / "OpenAdapt.app",
        )
        shutil.copy(
            ROOT_DIR / "build_scripts" / "macos.sh",
            ROOT_DIR / "dist" / "OpenAdapt.app" / "Contents" / "MacOS" / "OpenAdapt",
        )


def create_macos_dmg() -> None:
    """Create a DMG installer for macOS."""
    ROOT_DIR = Path(__file__).parent.parent
    subprocess.run(
        [
            "hdiutil",
            "create",
            "-volname",
            "OpenAdapt",
            "-srcfolder",
            ROOT_DIR / "dist" / "OpenAdapt.app",
            "-ov",
            "-format",
            "UDZO",
            ROOT_DIR / "dist" / "OpenAdapt.dmg",
        ]
    )


def download_and_install_inno_setup() -> None:
    """Download and install Inno Setup if not already installed."""
    inno_setup_url = "https://jrsoftware.org/download.php/is.exe"
    inno_setup_path = Path.home() / ".inno_setup" / "is.exe"

    if not inno_setup_path.exists():
        inno_setup_path.parent.mkdir(parents=True, exist_ok=True)
        print("Downloading Inno Setup...")
        urllib.request.urlretrieve(inno_setup_url, inno_setup_path)
        print("Installing Inno Setup...")
        subprocess.run([inno_setup_path, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/DIR={}".format(inno_setup_path.parent)], check=True)


def create_windows_installer() -> None:
    """Create an EXE installer for Windows using Inno Setup."""
    download_and_install_inno_setup()

    INNO_SETUP_SCRIPT = """
[Setup]
AppName=OpenAdapt
AppVersion=1.0
DefaultDirName={pf}\\OpenAdapt
DefaultGroupName=OpenAdapt
OutputBaseFilename=OpenAdapt_Installer
Compression=lzma
SolidCompression=yes

[Files]
Source: "dist\\OpenAdapt\\*"; DestDir: "{app}";
Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\\OpenAdapt"; Filename: "{app}\\OpenAdapt.exe"
Name: "{group}\\{cm:UninstallProgram,OpenAdapt}"; Filename: "{uninstallexe}"
"""
    INNO_SETUP_PATH = Path("build_scripts") / "OpenAdapt.iss"
    INNO_SETUP_PATH.write_text(INNO_SETUP_SCRIPT)

    subprocess.run(["iscc", INNO_SETUP_PATH])


def main() -> None:
    """Entry point."""
    build_pyinstaller()
    if sys.platform == "darwin":
        create_macos_dmg()
    elif sys.platform == "win32":
        create_windows_installer()


if __name__ == "__main__":
    main()
