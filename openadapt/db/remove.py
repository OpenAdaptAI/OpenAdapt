"""Removes recordings from the database.

Usage: python -m openadapt.db.remove [--all | --latest | --id <recording_id>]
"""
from sys import stdout

from loguru import logger
import click

from openadapt.db.crud import delete_recording, get_all_recordings

print()  # newline


@click.command()
@click.option("--all", is_flag=True, help="Remove all recordings.")
@click.option("--latest", is_flag=True, help="Remove the latest recording.")
@click.option("--id", "recording_id", type=int, help="Remove recording by ID.")
def remove(all: str, latest: str, recording_id: int) -> int:
    """Removes a recording from the database."""
    recordings = get_all_recordings()[::-1]

    logger.remove()
    logger.add(
        stdout,
        colorize=True,
        format="<blue>[DB]          </blue><yellow>{message}</yellow>",
    )

    if sum([all, latest, recording_id is not None]) > 1:
        logger.error("Invalid usage.")
        logger.error("Use --help for more information.")
        return 1

    if not recordings:
        logger.error("No recordings found.")
        return 1

    if all:
        if click.confirm("Are you sure you want to delete all recordings?"):
            for r in recordings:
                logger.info(f"Deleting {r.task_description} | {r.timestamp}...")
                delete_recording(r.timestamp)
            logger.info("All recordings deleted.")
        else:
            logger.info("Aborting...")
        return 0

    if latest:
        recording_id = len(recordings)

    elif recording_id is None or not (1 <= recording_id <= len(recordings)):
        logger.error("Invalid recording ID.")
        return 1
    recording_to_delete = recordings[recording_id - 1]

    if click.confirm(
        "Are you sure you want to delete recording"
        f" {recording_to_delete.task_description} | {recording_to_delete.timestamp}?"
    ):
        delete_recording(recording_to_delete.timestamp)
        logger.info("Recording deleted.")
    else:
        logger.info("Aborting...")
    return 0


if __name__ == "__main__":
    remove()
