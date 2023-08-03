from openadapt.crud import *
from openadapt import models
from copy import deepcopy
from loguru import logger
import utils


def condense_window_state(recording_id):
    
    grab_recording = get_recording_by_id(recording_id)
    curr_acx = get_action_events(grab_recording)

    for action in curr_acx:
        processed_acx, processed_wd = sanitize(action)[0], sanitize(action)[1]
        logger.debug(f"{processed_acx=}")
        logger.debug(f"{processed_wd=}")

        break

        processed_wd_axvalue = grab_ax_value(processed_wd)
        task_description = recording.task_description
        summary_dataset(processed_wd_axvalue, task_description)

    # get task description


def grab_ax_value(window_state):
    return window_state["AXValue"]


def summary_dataset(processed_wd_axvalue, task_desciption: str):
    pass
    # write to a json file with format
    # {prompt: axvalue, completion: task_description}


def finetune_on_summary(prompt: str, completion: str):
    # prompt is the current ACTION's PAIRED WINDOW STATE's AXValue,
    # and completion is the task description of the recording

    pass
    # read each line in json file, and train model on it.


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
    return reference_action_dicts, reference_window_dict
