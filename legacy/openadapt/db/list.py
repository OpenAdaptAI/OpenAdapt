"""Lists all recordings in the database.

Usage: python -m openadapt.db.list
"""

from sys import stdout

from openadapt.custom_logger import logger
from openadapt.db import crud


def main() -> None:
    """Prints all recordings in the database."""
    logger.remove()
    logger.add(
        stdout,
        colorize=True,
        format="<blue>[DB]          </blue><yellow>{message}</yellow>",
    )
    print()  # newline

    session = crud.get_new_session(read_only=True)
    recordings = crud.get_all_recordings(session)

    if not recordings:
        logger.info("No recordings found.")

    for idx, recording in enumerate(recordings[::-1], start=1):
        logger.info(
            f"[{idx}]: {recording.task_description} | {recording.timestamp}"
            + (" [latest]" if idx == len(recordings) else "")
        )


if __name__ == "__main__":
    main()
