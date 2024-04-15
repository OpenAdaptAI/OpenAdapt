"""This module contains the functions to run the dashboard web application."""


import os
import subprocess

from loguru import logger

from openadapt.extensions.thread import Thread


def run() -> Thread:
    """Run the dashboard web application."""
    # change to the client directory
    cur_dir = os.path.dirname(os.path.abspath(__file__))

    def run_client() -> subprocess.Popen:
        """The entry point for the thread that runs the dashboard client."""
        from openadapt.config import DASHBOARD_CLIENT_PORT, DASHBOARD_SERVER_PORT

        return subprocess.Popen(
            ["node", "index.js"],
            cwd=cur_dir,
            env={
                **os.environ,
                "DASHBOARD_CLIENT_PORT": str(DASHBOARD_CLIENT_PORT),
                "DASHBOARD_SERVER_PORT": str(DASHBOARD_SERVER_PORT),
            },
        )

    return Thread(target=run_client, daemon=True, args=())


def cleanup(process: subprocess.Popen) -> None:
    """Cleanup the dashboard web application process."""
    logger.debug("Terminating the dashboard client.")
    process.terminate()
    process.wait()
    logger.debug("Dashboard client terminated.")
