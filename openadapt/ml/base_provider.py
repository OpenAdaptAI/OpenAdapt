"""CompletionProvider and related classes."""

from typing import Any

from pydantic import BaseModel


class Modality:
    """LLM Modality class."""

    TEXT = "TEXT"
    IMAGE = "IMAGE"


class Use:
    """LLM Use Class."""

    COMMERCIAL_USE = "COMMERCIAL"
    NON_COMMERCIAL_USE = "NON_COMMERCIAL"


class Capability:
    """LLM Capability Class."""

    TRAINING = "TRAINING"
    TUNING = "TUNING"
    INFERENCE = "INFERENCE"


class Availability:
    """LLM Availability Class."""

    LOCAL_CPU = "LOCAL_CPU"
    LOCAL_GPU = "LOCAL_GPU"
    HOSTED = "HOSTED"


class CompletionProvider(BaseModel):
    """Base CompletionProvider class."""

    Name: str
    Capabilities: list[Capability]
    Modalities: list[Modality]
    Availabilities: list[Availability]

    class Config:  # pylint: disable=too-few-public-methods
        """Pydantic Config Class."""

        arbitrary_types_allowed = True

    def infer(prompt: str, model_name: str) -> None:
        """Run inference on the provider."""
        raise NotImplementedError

    def finetune(prompt_completion_pair: list[dict[str]]) -> None:
        """Fine-tune the provider."""
        raise NotImplementedError

    def get_children() -> Any:
        """Grab all completion provider classes."""
        return CompletionProvider.__subclasses__()


class CompletionProviderFactory:
    """CompletionProviderFactory class."""

    @staticmethod
    def get_for_modality(modality: Modality) -> list[CompletionProvider]:
        """Gets all available CompletionProviders with the given modality."""
        completion_providers = CompletionProvider.get_children()
        filtered_providers = [
            provider()
            for provider in completion_providers
            if modality in provider().Modalities
        ]
        return filtered_providers
