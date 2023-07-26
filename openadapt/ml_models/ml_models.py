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
    }
]


def select_ml_model(recording_id):
    recording_data = get_recording_by_id(recording_id)
    assert recording_data, "No such recording exists"
    # TODO:
    # for model in ML_MODELS:
    #    # use wandb for metrics on different ml models.

