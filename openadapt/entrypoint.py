"""Entrypoint for OpenAdapt."""

import multiprocessing

if __name__ == "__main__":
    # This needs to be called before any code that uses multiprocessing
    multiprocessing.freeze_support()

from loguru import logger

from openadapt.build_utils import redirect_stdout_stderr
from openadapt.splashscreen import show_splash_screen , exit_splash_screen

def run_openadapt() -> None:
    """Run OpenAdapt."""
    show_splash_screen()
    with redirect_stdout_stderr():
        try:
            from openadapt.alembic.context_loader import load_alembic_context
            from openadapt.app import tray
            from openadapt.config import print_config

            print_config()
            load_alembic_context()
            exit_splash_screen() 
            tray._run()
        except Exception as exc:
            logger.exception(exc)


if __name__ == "__main__":
    run_openadapt()
