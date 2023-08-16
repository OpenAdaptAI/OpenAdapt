from openadapt.ml.fine_tuning.base_finetuner import FineTuner
from typing import Optional
import subprocess

class OpenAIFineTuner(FineTuner):
    def __init__(self, model_name) -> None:
        super().__init__()
        self.model_name = model_name

    def prepare_data_for_tuning(self, file_path: str) -> str:
        """
        Prepare the data for fine-tuning the model. The data should be
        stored in correct format in the given file path.

        The output data is stored in a temporary file and the path to the
        file is returned.
        """
        output = subprocess.run(
            ["openai", "tools", "fine_tunes.prepare_data", "-f", file_path],
            capture_output=True,
        )
        return output.stdout.decode("utf-8")
    
    def tune_model(self, fine_tune_data_path: Optional[str] = None):
        output = subprocess.run(
            ["openai", "api", "fine_tunes.create", "-t", fine_tune_data_path, "-m", self.model_name],
            capture_output=True,
        )
        return output.stdout.decode("utf-8")

"""
def write_to_file(recording_id: int):
    
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
"""