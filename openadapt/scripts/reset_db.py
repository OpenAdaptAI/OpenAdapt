import os
from subprocess import run, PIPE
from openadapt import config


def reset_db():
    """
    The function clears the database by removing the database file and running a
    database migration using Alembic.
    """
    if os.path.exists(config.DB_FPATH):
        os.remove(config.DB_FPATH)

    # Prevents duplicate logging of config values by piping stderr and filtering the output.
    result = run(["alembic", "upgrade", "head"], stderr=PIPE, text=True)
    print(result.stderr[result.stderr.find("INFO  [alembic") :])
    if result.returncode != 0:
        raise RuntimeError("Database migration failed.")
    else:
        print("Database cleared successfully.")


if __name__ == "__main__":
    reset_db()
