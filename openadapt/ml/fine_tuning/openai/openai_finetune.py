from openadapt.ml.fine_tuning.base_finetuner import FineTuner
from typing import Optional
import json
import subprocess

import os


class OpenAIFineTuner(FineTuner):
    def __init__(self, model_name: str) -> None:
        super().__init__()
        self.model_name = model_name

    def check_data_for_tuning(self, file_path: str) -> None:
        """
        Prepare the data for fine-tuning the model. The data should be
        stored in correct format in the given file path.

        The output data is stored in a temporary file and the path to the
        file is returned.
        """

        subprocess.run(["pip3", "install", "openai"])

        output = subprocess.run(
            ["openai", "tools", "fine_tunes.prepare_data", "-f", file_path],
            capture_output=True,
            text=True
        )
        return output

    def tune_model(self, fine_tune_data_path: Optional[str] = None) -> None:
        output = subprocess.run(
            [
                "openai",
                "api",
                "fine_tunes.create",
                "-t",
                fine_tune_data_path,
                "-m",
                self.model_name,
            ],
        )

        return output

    def prepare_data_for_tuning(self, recording_id: int) -> str:
        """
        Users must invoke this to prepare their data into a JSONL file
        formatted according to OpenAI Finetune standards. The function
        returns the file path which can be passed into check_data_for_tuning
        to confirm that it has been formatted properly.
        """
        condensed_recording = self.data_processor._condense_data(recording_id)

        file_path = generate_file_path(recording_id)

        recording_file = open(file_path, "x")
        recording_file.close()
        with open(
            file_path,
            mode="w",
        ) as json_file:
            for curr_event, next_event in zip(
                condensed_recording, condensed_recording[1:]
            ):
                curr_acx = curr_event[0][0]
                curr_win = curr_event[1]

                next_acx = next_event[0][0]
                next_win = next_event[1]
                paired_dict = {
                    "prompt": f"{(curr_acx, curr_win)}",
                    "completion": f" {(next_acx,next_win)}",
                }
                json_file.write(json.dumps(paired_dict))
                json_file.write("\n")

            json_file.close()

        return file_path


def generate_file_path(recording_id: int) -> str:
    return f"{recording_id}_processed.jsonl"
