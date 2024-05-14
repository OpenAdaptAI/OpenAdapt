"""This module contains the functions to run the dashboard web application."""


from threading import Thread
import os
import pathlib
import subprocess
import webbrowser

from loguru import logger

from openadapt.build_utils import is_running_from_executable
from openadapt.config import POSTHOG_HOST, POSTHOG_PUBLIC_KEY, config

from .api.index import run_app

dashboard_process = None


def run() -> Thread:
    """Run the dashboard web application."""
    # change to the client directory
    cur_dir = pathlib.Path(__file__).parent

    def run_client() -> subprocess.Popen:
        """The entry point for the thread that runs the dashboard client."""
        if is_running_from_executable():
            webbrowser.open(
                f"http://localhost:{config.DASHBOARD_SERVER_PORT}/recordings"
            )
            run_app()
            return

        global dashboard_process
        dashboard_process = subprocess.Popen(
            ["node", "index.js"],
            cwd=cur_dir,
            env={
                **os.environ,
                "DASHBOARD_CLIENT_PORT": str(config.DASHBOARD_CLIENT_PORT),
                "DASHBOARD_SERVER_PORT": str(config.DASHBOARD_SERVER_PORT),
                "NEXT_PUBLIC_POSTHOG_HOST": POSTHOG_HOST,
                "NEXT_PUBLIC_POSTHOG_PUBLIC_KEY": POSTHOG_PUBLIC_KEY,
                "NEXT_PUBLIC_MODE": (
                    "production" if is_running_from_executable() else "development"
                ),
            },
        )

    return Thread(
        target=run_client,
        daemon=True,
        args=(),
    )


def cleanup() -> None:
    """Cleanup the dashboard web application process."""
    logger.debug("Terminating the dashboard client.")
    global dashboard_process
    if dashboard_process:
        dashboard_process.terminate()
        dashboard_process.wait()
    logger.debug("Dashboard client terminated.")


if __name__ == "__main__":
    dashboard_thread = run()
    dashboard_thread.start()
    try:
        while True:
            pass
    except KeyboardInterrupt:
        cleanup()
