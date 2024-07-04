"""Consolidated post-install script for OpenAdapt."""

import os
import subprocess
import sys


def install_detectron2() -> None:
    """Install Detectron2 from its GitHub repository."""
    try:
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
    except subprocess.CalledProcessError as e:
        print(f"Error installing Detectron2: {e}")
        sys.exit(1)


def install_dashboard() -> None:
    """Install dashboard dependencies based on the operating system."""
    original_directory = os.getcwd()
    print(f"Original directory: {original_directory}")

    dashboard_dir = os.path.join(original_directory, "openadapt", "app", "dashboard")
    print(f"Dashboard directory: {dashboard_dir}")

    if not os.path.exists(os.path.join(dashboard_dir, "package.json")):
        print("package.json not found in the dashboard directory.")
        sys.exit(1)

    try:
        os.chdir(dashboard_dir)
        print("Changed directory to:", os.getcwd())

        if sys.platform.startswith("win"):
            try:
                subprocess.check_call(["nvm", "install", "21"])
                subprocess.check_call(["nvm", "use", "21"])
            except FileNotFoundError:
                if os.getenv("CI") == "true":
                    print("nvm not found. Skipping installation.")
                else:
                    print("nvm not found. Please install nvm.")
                    sys.exit(1)
            subprocess.check_call(["powershell", "./entrypoint.ps1"])
        else:
            subprocess.check_call(["bash", "./entrypoint.sh"])
    except subprocess.CalledProcessError as e:
        print(f"Error running entrypoint script: {e}")
        sys.exit(1)
    finally:
        os.chdir(original_directory)
        print("Reverted to original directory:", os.getcwd())


def main() -> None:
    """Main function to install dependencies."""
    try:
        install_detectron2()
        install_dashboard()
    except Exception as e:
        print(f"Unhandled error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
