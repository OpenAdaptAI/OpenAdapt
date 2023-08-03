from openadapt.crud import *
from openadapt import models, utils
from copy import deepcopy
from loguru import logger

from openadapt.strategies.stateful import get_window_state_diffs

def condense_window_state(recording_id):

    grab_recording = get_recording_by_id(recording_id)
    curr_acx = get_action_events(grab_recording)

    diffs = get_window_state_diffs(curr_acx)
    logger.debug(f"{diffs=}")
    return

    for action in curr_acx:
        processed_acx, processed_wd = sanitize(action)[0], sanitize(action)[1]
        logger.debug(f"{processed_acx=}")
        logger.debug(f"{processed_wd=}")

        break
        task_description = recording.task_description
        summary_dataset(processed_wd_axvalue, task_description)

    # get task description

# What to fine tune on

# if we finetune on actions, then the model can perform
# actions on windows where these actions are meaningless. 
# => BAD IDEA.

# things that are a problem: length of reference window states.

# right now, we give it reference window and action dict for ONE TASK 

# then we give it an active window state and ask it to replicate these 
# actions on the current window.


# we absolutely NEED the coordinates of the generated action
# to match the ones in the active window state.

# SINCE the window doesnt change, how about just describing the 
# translation, and saving up on that length?

# then diffs for the reference window state. 
########################################################################
# Goal: we wish to enhance statefulreplaystrat to work properly.


def summary_dataset(processed_wd_axvalue, task_desciption: str):
    pass
    # write to a json file with format
    # {prompt: axvalue, completion: task_description}


def finetune_on_summary(prompt: str, completion: str):
    # prompt is the current ACTION's PAIRED WINDOW STATE,
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
    reference_window_dict.pop("left")
    reference_window_dict.pop("top")
    reference_window_dict.pop("width")
    reference_window_dict.pop("height")
    return reference_action_dicts, reference_window_dict


if __name__ == "__main__":
    condense_window_state(2)