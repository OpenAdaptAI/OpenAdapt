"""
Implements the code needed to update the OpenAdapt app
if needed.

Usage:
    python3 -m openadapt.start
"""
from loguru import logger
import subprocess

# from openadapt.app.main import run_app


def changes_needed(result: str) -> bool:
    """Checks if git stash needs to be run.

    Assumes result is the result of git status.
    """
    if "Changes to be committed:" in result:
        return True
    elif "Changes not staged for commit:" in result:
        return True
    return False


def start_openadapt_app() -> None:
    """
    The main function which runs the OpenAdapt app when it is updated.
    """
    result = subprocess.run(["git", "status"], capture_output=True, text=True)

    if changes_needed(result.stdout):
        subprocess.run(["git", "stash"])

    elif "branch is up to date" not in result.stdout:
        pull_result = subprocess.run(["git", "pull", "-q"])
        if "diverged" in pull_result.stdout:
            print("Please fix merge conflicts and try again")
            return
        print("Updated the OpenAdapt App")

    # run_app()  # start gui


if __name__ == "__main__":
    start_openadapt_app()
