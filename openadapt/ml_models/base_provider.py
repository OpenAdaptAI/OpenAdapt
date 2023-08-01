from openadapt import models
from pydantic import BaseModel

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

class Availability:
    LOCAL_CPU = "LOCAL_CPU"
    LOCAL_GPU = "LOCAL_GPU"
    HOSTED = "HOSTED"
    
class CompletionProvider(BaseModel):
    Name: str
    Capabilities: list[Capability]
    Modalities: list[Modality]
    Availabilities: list[Availability]

    def infer(self, prompt: str):
        raise NotImplementedError

    def finetune(self, prompt: str, completion: str):
        raise NotImplementedError

    def get_children(self):
        return CompletionProvider.__subclasses__()