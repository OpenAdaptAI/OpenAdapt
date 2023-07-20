from openadapt import models
from openadapt import ml_models
from ml_models.openai import gpt3_davinci
from openadapt.crud import get_recording_by_id


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
        "provider": gpt3_davinci,
        "finetune": gpt3_davinci.finetune,
        "infer": gpt3_davinci.infer,
        "modalities": gpt3_davinci.MODALITIES,
    }
]


def select_ml_model(recording_id):
    recording_data = get_recording_by_id(recording_id)
    assert recording_data, "No such recording exists"
    # TODO:
    # for model in ML_MODELS:
    #    # use wandb for metrics on different ml models. 
