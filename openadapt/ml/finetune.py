from openadapt.crud import *
from openadapt import models, utils
from copy import deepcopy
import json
from loguru import logger

from openadapt.strategies.stateful import get_window_state_diffs


def condense_window_state(recording_id: int):
    """
    Takes in a recording as input and outputs a list of tuples
    of action, window events where window events are condensed
    to contain basic information, as in stateful_test.py
    """
    grab_recording = get_recording_by_id(recording_id)
    total_acx = get_action_events(grab_recording)

    for i in range(len(total_acx)):
        processed_acx, processed_wd = (
            sanitize(total_acx[i])[0],
            sanitize(total_acx[i])[1],
        )
        processed_wd.pop("meta")
        # task_description = recording.task_description

        logger.debug(f"{total_acx[i].timestamp=}")
        logger.debug(f"{total_acx[i].recording_timestamp=}")
        logger.debug(f"{total_acx[i].screenshot_timestamp=}")
        logger.debug(f"{total_acx[i].window_event_timestamp=}")
        # which timestamp to finetune on? 
        total_acx[i] = (processed_acx, processed_wd)
    return total_acx


def finetune(recording_id: int):
    condensed_recording = condense_window_state(recording_id)

    # for curr_acx, curr_window in condensed_recording:
    #   curr_pair= f'action:{curr_acx}, window: {curr_window}'
    #   pair_json = json.loads(curr_pair)

    # finetune on action timestamp?
    # so {prompt: (A,W)_{k}, completion: (A,W)_{k+1}}?


def sanitize(action):
    # taken from statefulreplaystrat
    reference_window = action.window_event

    reference_window_dict = deepcopy(
        {
            key: val
            for key, val in utils.row2dict(reference_window, follow=False).items()
            if val is not None
            and not key.endswith("timestamp")
            and not key.endswith("id")
            # and not isinstance(getattr(models.WindowEvent, key), property)
        }
    )
    if action.children:
        reference_action_dicts = [
            deepcopy(
                {
                    key: val
                    for key, val in utils.row2dict(child, follow=False).items()
                    if val is not None
                    and not key.endswith("timestamp")
                    and not key.endswith("id")
                    and not isinstance(getattr(models.ActionEvent, key), property)
                }
            )
            for child in action.children
        ]
    else:
        reference_action_dicts = [
            deepcopy(
                {
                    key: val
                    for key, val in utils.row2dict(action, follow=False).items()
                    if val is not None
                    and not key.endswith("timestamp")
                    and not key.endswith("id")
                }
            )
        ]
    reference_window_dict["state"].pop("data")
    return reference_action_dicts, reference_window_dict["state"]


if __name__ == "__main__":
    condense_window_state(2)
