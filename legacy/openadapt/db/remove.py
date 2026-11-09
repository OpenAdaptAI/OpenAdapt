"""Removes recordings from the database.

Usage: python -m openadapt.db.remove [--all | --latest | --id <recording_id>]
"""

from sys import stdout

import click

from openadapt.custom_logger import logger
from openadapt.db import crud

print()  # newline


@click.command()
@click.option("--all", is_flag=True, help="Remove all recordings.")
@click.option("--latest", is_flag=True, help="Remove the latest recording.")
@click.option("--id", "recording_id", type=int, help="Remove recording by ID.")
def remove(all: str, latest: str, recording_id: int) -> int:
    """Removes a recording from the database."""
    if not crud.acquire_db_lock():
        logger.error("Failed to acquire database lock.")
        return 1

    def cleanup(return_code: int) -> int:
        """Releases the database lock and returns the given return code.

        Args:
            return_code (int): The return code to return.

        Returns:
            return_code (int): The given return code.
        """
        crud.release_db_lock()
        return return_code

    read_only_session = crud.get_new_session(read_only=True)
    recordings = crud.get_all_recordings(read_only_session)[::-1]

    logger.remove()
    logger.add(
        stdout,
        colorize=True,
        format="<blue>[DB]          </blue><yellow>{message}</yellow>",
    )

    if sum([all, latest, recording_id is not None]) > 1:
        logger.error("Invalid usage.")
        logger.error("Use --help for more information.")
        return cleanup(1)

    if not recordings:
        logger.error("No recordings found.")
        return cleanup(1)

    if all:
        if click.confirm("Are you sure you want to delete all recordings?"):
            with crud.get_new_session(read_and_write=True) as write_session:
                for r in recordings:
                    logger.info(f"Deleting {r.task_description} | {r.timestamp}...")
                    crud.delete_recording(write_session, r)
            logger.info("All recordings deleted.")
        else:
            logger.info("Aborting...")
        return cleanup(0)

    if latest:
        recording_id = len(recordings)

    elif recording_id is None or not (1 <= recording_id <= len(recordings)):
        logger.error("Invalid recording ID.")
        return cleanup(1)
    recording_to_delete = recordings[recording_id - 1]

    if click.confirm(
        "Are you sure you want to delete recording"
        f" {recording_to_delete.task_description} | {recording_to_delete.timestamp}?"
    ):
        with crud.get_new_session(read_and_write=True) as write_session:
            crud.delete_recording(write_session, recording_to_delete)
        crud.release_db_lock()
        logger.info("Recording deleted.")
    else:
        logger.info("Aborting...")
    return cleanup(0)


if __name__ == "__main__":
    remove()
