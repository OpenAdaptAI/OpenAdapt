from openadapt import models
import openai_module
from openai_module.gpt3_davinci import infer, finetune, MODALITIES
from openadapt.crud import (
    get_recording_by_id,
    get_latest_recording,
    get_window_events,
    get_action_events,
)
from loguru import logger 


class Modality:
    TEXT = "TEXT"
    IMAGE = "IMAGE"

class License:
    COMMERCIAL_USE = "COMMERCIAL_USE"
    NON_COMMERCIAL_USE = "NON_COMMERCIAL_USE"

class Capability:
    TRAINING = "TRAINING"
    TUNING = "TUNING"
    USABLE_OUTPUT = "USABLE_OUTPUT"
    INFERENCE = "INFERENCE"

MODALITY_BY_DB_MODEL = {
    models.ActionEvent: Modality.TEXT,
    models.Screenshot: Modality.IMAGE,
    models.WindowEvent: Modality.IMAGE,
}

ML_MODELS = [
    {
        "name": "gpt3_davinci",
        "provider": openai_module.gpt3_davinci,
        "finetune": finetune,
        "infer": infer,
        "modalities": MODALITIES,
        "context_length": 4096,
        "license": License.COMMERCIAL_USE,
        "capability": [Capability.TRAINING, Capability.TUNING, Capability.USABLE_OUTPUT, Capability.INFERENCE]
    }
]


def select_ml_model(recording_id):
    recording_data = get_recording_by_id(recording_id)
    assert recording_data, "No such recording exists"
    # TODO:
    # for model in ML_MODELS:
    #    # use wandb for metrics on different ml models.


def add_model(name: str, provider: str):
    """
    High level function to add a new model to the list of ML modes
    """