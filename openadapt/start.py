"""Implements the code necessary to update the OpenAdapt app if required.

Usage:
    python3 -m openadapt.start
"""

import subprocess

from openadapt.custom_logger import logger


def main() -> None:
    """The main function which runs the OpenAdapt app when it is updated."""
    result = subprocess.run(["git", "status"], capture_output=True, text=True)

    if "unmerged" in result.stdout:
        logger.info("Please fix merge conflicts and try again")
        return

    subprocess.run(["git", "stash"])

    if "git pull" in result.stdout:
        subprocess.run(["git", "pull", "-q"])
        logger.info("Updated the OpenAdapt App")


if __name__ == "__main__":
    main()
