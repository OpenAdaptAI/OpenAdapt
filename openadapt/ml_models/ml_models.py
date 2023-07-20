from openadapt import models
from openadapt import ml_models
from ml_models.openai import gpt4
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
        "name": "gpt-4",
        "provider": gpt4,
        "finetune": gpt4.finetune,
        "infer": gpt4.infer,
        "modalities": gpt4.MODALITIES,
    }
]


def select_ml_model(recording_id):
    recording_data = get_recording_by_id(recording_id)
    assert recording_data, "No such recording exists"
