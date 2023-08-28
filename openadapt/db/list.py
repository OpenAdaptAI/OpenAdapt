"""Lists all recordings in the database.

Usage: python -m openadapt.db.list
"""

from sys import stdout

from loguru import logger

from openadapt.db.crud import get_all_recordings


def main() -> None:
    """Prints all recordings in the database."""
    logger.remove()
    logger.add(
        stdout,
        colorize=True,
        format="<blue>[DB]          </blue><yellow>{message}</yellow>",
    )
    print()  # newline

    if not get_all_recordings():
        logger.info("No recordings found.")

    for idx, recording in enumerate(get_all_recordings()[::-1], start=1):
        logger.info(
            f"[{idx}]: {recording.task_description} | {recording.timestamp}"
            + (" [latest]" if idx == len(get_all_recordings()) else "")
        )


if __name__ == "__main__":
    main()
