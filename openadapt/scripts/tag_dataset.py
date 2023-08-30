"""Script for tagging a screenshot and window event as a dataset entry.

Usage:

    $ python openadapt/scripts/tag_dataset.py <window_event_timestamp>
    <screenshot_timestamp> [<dataset_id>]

"""

from typing import Optional

from loguru import logger
import fire

from openadapt.crud import (
    get_dataset,
    get_screenshot,
    get_window_event,
    insert_dataset,
    insert_dataset_entry,
)


def tag_data(
    window_event_timestamp: float,
    screenshot_timestamp: float,
    dataset_id: Optional[int] = None,
) -> None:
    """Create a DatasetEntry in the database.

    This entry will refer to the window event and screenshot with the
    given timestamps, and will be part of the dataset with the given ID.

    Args:
        window_event_timestamp (float): The timestamp of the desired window event.
        screenshot_timestamp (float): The timestamp of the desired screenshot.
        dataset_id (Optional[int] = None): The id of the dataset to add to.

    """
    if dataset_id is None:
        dataset = insert_dataset()
        dataset_id = dataset.id
    else:
        # check if given ID is valid
        dataset = get_dataset(dataset_id)
        if dataset is None:
            logger.info(
                "Please provide a valid dataset ID to add to an existing dataset, "
                "or do not provide a dataset ID and create a new dataset."
            )
            return

    # check if the given timestamps are valid
    screenshot = get_screenshot(screenshot_timestamp)
    window_event = get_window_event(window_event_timestamp)
    if screenshot is None:
        logger.info("Please provide the timestamp of an existing screenshot.")
        return
    if window_event is None:
        logger.info("Please provide the timestamp of an existing window event.")
        return

    # create a DatasetEntry with given parameters
    dataset_entry_data = {
        "dataset_id": dataset_id,
        "window_event_timestamp": window_event_timestamp,
        "screenshot_timestamp": screenshot_timestamp,
    }
    insert_dataset_entry(dataset_entry_data)
    logger.info("Data tagged successfully!")


if __name__ == "__main__":
    fire.Fire(tag_data)
