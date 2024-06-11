"""Consolidated post-install script for OpenAdapt."""

import subprocess
import sys
import os


def install_detectron2() -> None:
    """Install Detectron2 from its GitHub repository."""
    subprocess.check_call(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "git+https://github.com/facebookresearch/detectron2.git",
            "--no-build-isolation",
        ]
    )


def install_dashboard() -> None:
    """Install dashboard dependencies based on the operating system."""
    if sys.platform.startswith("win"):
        # For Windows, use PowerShell script
        subprocess.check_call([
            "powershell",
            os.path.join("openadapt", "app", "dashboard", "entrypoint.ps1"),
        ])
    else:
        # For Unix-like systems, use Bash script
        subprocess.check_call([
            "bash",
            os.path.join("openadapt", "app", "dashboard", "entrypoint.sh"),
        ])


def main() -> None:
    """Main function to install dependencies."""
    install_detectron2()
    install_dashboard()


if __name__ == "__main__":
    main()
