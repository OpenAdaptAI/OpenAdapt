from openadapt import models
from openadapt import ml_models
from openadapt.crud import get_recording_by_id
class Modality:
    TEXT = "TEXT"
    IMAGE= "IMAGE"

MODALITY_BY_DB_MODEL = {
    models.ActionEvent: Modality.TEXT,
    models.Screenshot: Modality.IMAGE,
    models.WindowEvent:  Modality.IMAGE,
}

ML_MODELS = [

]


def select_ml_model(recording_id):
    recording_data = get_recording_by_id(recording_id)
    assert recording_data, "No such recording exists"
    