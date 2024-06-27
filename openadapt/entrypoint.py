"""Entrypoint for OpenAdapt."""

import multiprocessing

if __name__ == "__main__":
    # This needs to be called before any code that uses multiprocessing
    multiprocessing.freeze_support()

from datetime import datetime

from openadapt.build_utils import get_root_dir_path, redirect_stdout_stderr


def run_openadapt() -> None:
    """Run OpenAdapt."""
    with redirect_stdout_stderr():
        try:
            from openadapt.alembic.context_loader import load_alembic_context
            from openadapt.app import tray
            from openadapt.config import print_config

            print_config()
            load_alembic_context()
            tray._run()
        except Exception as exc:
            # TODO: log all exceptions to a file
            print(f"{datetime.now()}: {exc}")


if __name__ == "__main__":
    run_openadapt()
