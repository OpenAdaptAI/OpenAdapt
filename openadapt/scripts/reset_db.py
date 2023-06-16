import os
from subprocess import run, PIPE
from openadapt.config import getenv_fallback


def reset_db():
    """
    The function clears the database by removing the database file and running a
    database migration using Alembic.
    """
    db = getenv_fallback("DB_FNAME")
    if os.path.exists(db):
        os.remove(db)

    # Prevents duplicate logging of config values by piping stderr and filtering the output.
    result = run(["alembic", "upgrade", "head"], stderr=PIPE, text=True)
    print(result.stderr[result.stderr.find("INFO  [alembic") :])
    if result.returncode != 0:
        raise RuntimeError("Database migration failed.")
    else:
        print("Database cleared successfully.")


if __name__ == "__main__":
    reset_db()
