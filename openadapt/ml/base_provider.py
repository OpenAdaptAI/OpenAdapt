from openadapt import models
from pydantic import BaseModel


class Modality:
    TEXT = "TEXT"
    IMAGE = "IMAGE"


class Use:
    COMMERCIAL_USE = "COMMERCIAL"
    NON_COMMERCIAL_USE = "NON_COMMERCIAL"


class Capability:
    TRAINING = "TRAINING"
    TUNING = "TUNING"
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

    class Config:  # pylint: disable=too-few-public-methods
        """Pydantic Config Class."""

        arbitrary_types_allowed = True

    def infer(prompt: str, model_name: str):
        raise NotImplementedError

    def finetune(prompt_completion_pair: list[dict[str]]):
        raise NotImplementedError

    def get_children():
        return CompletionProvider.__subclasses__()


class CompletionProviderFactory:
    def get_for_modality(modality: Modality) -> list[CompletionProvider]:
        completion_providers = CompletionProvider.get_children()
        filtered_providers = [
            provider()
            for provider in completion_providers
            if modality in provider().Modalities
        ]
        return filtered_providers
