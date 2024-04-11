from contextlib import redirect_stderr, redirect_stdout
import multiprocessing


def run_openadapt():
    if __name__ == "__main__":
        multiprocessing.freeze_support()
    from openadapt.alembic.context_loader import load_context
    from openadapt.app import tray  # noqa
    from openadapt.config import print_config

    if __name__ == "__main__":
        print_config()
        load_context()
        tray._run()


with open("openadapt.stdout.log", "w") as stdout, redirect_stdout(stdout):
    with open("openadapt.stderr.log", "w") as stderr, redirect_stderr(stderr):
        run_openadapt()
