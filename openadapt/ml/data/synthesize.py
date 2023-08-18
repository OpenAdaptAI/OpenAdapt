"""
Script for creating vision dataset of screenshots and window states taken from a recording.

Usage:

    $ python -m openadapt.ml.data.synthesize

"""

import json
import os

from openadapt.crud import get_latest_recording, get_window_events

PATH_TO_IMAGES = "openadapt/ml/data/vision_dataset/images"
PATH_TO_JSON = "openadapt/ml/data/vision_dataset/states.json"


def synthesize() -> None:
    """
    Creates dataset of screenshots and their respective window states from the latest recording.
    Filters out unneeded info from window states (meta, data, window_id).
    Saves screenshot with a unique ID, and saves the window states along with the ID to a JSON file.
    """
    recording = get_latest_recording()
    window_events = get_window_events(recording)
    states = []

    # if this script hasn't been run yet, image_id starts at 1
    # if it has been run already, image_id starts at the max existing id + 1
    existing_ids = [
        int(filename.split(".")[0])
        for filename in os.listdir(PATH_TO_IMAGES)
        if filename.split(".")[0].isdigit()
    ]
    image_id = max(existing_ids, default=0) + 1

    # check if the json file already has contents
    if os.path.isfile(PATH_TO_JSON):
        with open(PATH_TO_JSON, "r") as json_file:
            existing_json = json.load(json_file)
    else:
        existing_json = []

    for event in window_events:
        state = event.state

        if state is None:
            # create state object using the event attributes
            state = {
                "title": event.title,
                "left": event.left,
                "top": event.top,
                "width": event.width,
                "height": event.height,
            }
        else:
            # filter unneeded info
            del state["meta"]
            del state["data"]
            del state["window_id"]

        # take last action event's screenshot
        ss = event.action_events[-1].screenshot.image
        ss.save(f"{PATH_TO_IMAGES}/{image_id}.jpg")
        new_state_entry = {"id": image_id, "window_state": state}
        states.append(new_state_entry)
        image_id += 1

    existing_json.extend(states)
    with open(PATH_TO_JSON, "w") as json_file:
        json.dump(existing_json, json_file)


if __name__ == "__main__":
    synthesize()
