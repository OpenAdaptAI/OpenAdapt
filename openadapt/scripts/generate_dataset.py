"""
Usage:

    $ python openadapt/scripts/generate_dataset.py <dataset_id>

"""

import json

from loguru import logger
import fire

from openadapt.crud import (
    get_dataset,
    get_dataset_entries,
    get_screenshot,
    get_window_event,
)

PATH_TO_IMAGES = "openadapt/ml/data/vision_dataset/images"
PATH_TO_JSON = "openadapt/ml/data/vision_dataset/states.json"


def generate_dataset(dataset_id: int) -> None:
    # check if valid dataset id
    dataset = get_dataset(dataset_id)
    if dataset is None:
        logger.info("Please provide a valid dataset ID.")
        return

    # get all dataset entries with dataset id
    dataset_entries = get_dataset_entries(dataset_id)
    states = []

    # loop through entries and save the images and add to the json
    for entry in dataset_entries:
        screenshot = get_screenshot(entry.screenshot_timestamp)
        window_event = get_window_event(entry.window_event_timestamp)
        state = window_event.state
        if state is None:
            # create state object using the event attributes
            state = {
                "title": window_event.title,
                "left": window_event.left,
                "top": window_event.top,
                "width": window_event.width,
                "height": window_event.height,
            }
        # TODO: scrub ss and event

        # TODO: check how mss images should be saved
        final_ss = screenshot.image
        final_ss.save(f"{PATH_TO_IMAGES}/{entry.id}.jpg")

        new_state_entry = {"id": entry.id, "window_state": state}
        states.append(new_state_entry)

    with open(PATH_TO_JSON, "w") as json_file:
        json.dump(states, json_file)

    logger.info("Dataset generated successfully!")


if __name__ == "__main__":
    fire.Fire(generate_dataset)
