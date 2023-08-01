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

class CompletionProvider(BaseModel):
    Name: str
    Capabilities: list[Capability]
    Modalities: list[Modality]

    def infer(self, prompt: str):
        raise NotImplementedError

    def finetune(self, prompt: str, completion: str):
        raise NotImplementedError