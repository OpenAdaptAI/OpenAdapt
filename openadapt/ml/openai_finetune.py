from openadapt.crud import *
from openadapt import models, utils
from copy import deepcopy
from loguru import logger
import json
import openai
import fire


def main(recording_id: int):
    write_to_file(recording_id)


def write_to_file(recording_id: int):
    """
    Creates a file for a new recording and writes
    the prompt, completion pairs to it.
    """
    condensed_recording = condense_window_state(recording_id)
    # for curr_acx, curr_window in condensed_recording:
    #   curr_pair= f'action:{curr_acx}, window: {curr_window}'
    #   pair_json = json.loads(curr_pair)
    recording_file = open(f"{recording_id}_processed.jsonl", "x")
    recording_file.close()
    with open(
        f"{recording_id}_processed.jsonl",
        mode="w",
    ) as json_file:
        for idx in range(len(condensed_recording) - 1):
            curr_acx = condensed_recording[idx][0][0]
            curr_win = condensed_recording[idx][1]

            next_acx = condensed_recording[idx + 1][0][0]
            next_win = condensed_recording[idx + 1][1]
            paired_dict = {
                "prompt": f"{(curr_acx, curr_win)}",
                "completion": f" {(next_acx,next_win)}",
            }
            # write this to a file
            # paired_dict_json = json.loads(str(paired_dict))
            json_file.write(json.dumps(paired_dict))
            json_file.write("\n")

        json_file.close()


def run_inference(incomplete_recording_id: int, finetuned_model):
    processed_recording = condense_window_state(incomplete_recording_id)
    openai.Completion.create(model=finetuned_model, prompt=processed_recording)


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
    fire.Fire(main)