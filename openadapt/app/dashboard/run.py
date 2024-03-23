import os
import subprocess

from loguru import logger

from openadapt.extensions.thread import Thread


def run():
    # change to the client directory
    cur_dir = os.path.dirname(os.path.abspath(__file__))

    def run_client():
        # run the client
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


def cleanup(process):
    logger.debug("Terminating the dashboard client.")
    process.terminate()
    process.wait()
    logger.debug("Dashboard client terminated.")
