from copy import deepcopy
from openadapt.models import Recording, ActionEvent
from openadapt import crud, utils
from typing import Optional, Any


class DataProcessor:
    def _retrieve_recording(self, recording_id: str) -> Recording:
        """
        Retrieve the recording from the database
        """
        return crud.get_recording_by_id(recording_id)

    def _condense_data(self, recording_id: int):
        """
        Condense the action events to remove any unnecessary information
        """
        grab_recording = crud.get_recording_by_id(recording_id)
        total_acx = crud.get_action_events(grab_recording)

        for i in range(len(total_acx)):
            processed_acx, processed_wd = (
                self._sanitize(total_acx[i])[0],
                self._sanitize(total_acx[i])[1],
            )
            processed_wd.pop("meta")
            # which timestamp to finetune on?
            total_acx[i] = (processed_acx, processed_wd)
        return total_acx

    def _sanitize(self, action):
        # Chunk of code taken from StatefulReplayStrategy,
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
                        and not isinstance(getattr(ActionEvent, key), property)
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

    def generate_data(self, recording_id: str) -> any:
        recording = self._retrieve_recording(recording_id)
        condensed_data = self._condense_data(recording)
        return condensed_data


class FineTuner:
    def __init__(self) -> None:
        self.data_processor = DataProcessor()

    def prepare_data_for_tuning(self, file_path: str) -> str:
        """
        Prepare the data for fine-tuning the model. The data should be
        stored in correct format in the given file path.

        The output data is stored in a temporary file and the path to the
        file is returned.
        """
        pass

    def tune_model(self, fine_tune_data_path: Optional[str] = None) -> any:
        """
        Fine tune the model with the given data.
        """
        pass

    def tune_model_autoregressively(self, recording_id: str):
        """
        Fine tune the model autoregressively on the given recording.
        """
        data = self.data_processor.generate_data(recording_id)
        # TODO: Save condensed data to a temporary file for fine-tuning
        # TODO: Fine-tune the model with the data
        prep_data_path = self.prepare_data_for_tuning("tmp/data/prompt.json")
        self.tune_model(prep_data_path)
        # TODO: maybe return the fine-tuned model name or fine tuned params
