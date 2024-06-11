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
    # Save the original directory to revert back after operations
    original_directory = os.getcwd()
    print(f"{original_directory=}")

    # Path to the dashboard directory containing package.json
    dashboard_dir = os.path.join("openadapt", "app", "dashboard")
    print(f"{dashboard_dir=}")

    # Ensure the dashboard directory exists and contains package.json
    if not os.path.exists(os.path.join(dashboard_dir, "package.json")):
        raise FileNotFoundError("package.json not found in the dashboard directory.")

    try:
        # Change the current working directory to the dashboard directory
        os.chdir(dashboard_dir)

        if sys.platform.startswith("win"):
            # For Windows, use PowerShell script
            subprocess.check_call(["powershell", "entrypoint.ps1"])
        else:
            # For Unix-like systems, use Bash script
            subprocess.check_call(["bash", "entrypoint.sh"])
    finally:
        # Revert back to the original directory regardless of success or failure
        os.chdir(original_directory)


def main() -> None:
    """Main function to install dependencies."""
    install_detectron2()
    install_dashboard()


if __name__ == "__main__":
    main()
