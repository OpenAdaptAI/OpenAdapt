"""Generic CompletionProvider module."""

from transformers import pipeline

from openadapt.ml.base_provider import (
    Availability,
    Capability,
    CompletionProvider,
    Modality,
)

# TODO: Implement HostedHuggingFaceProvider to support
# HF Inference Endpoints: https://huggingface.co/docs/huggingface_hub/guides/inference


class LocalHuggingFaceProvider(CompletionProvider):
    """GenericHuggingFaceProvider class."""

    Name: str = "Generic HuggingFace Provider"
    Capabilities: list[Capability] = [
        Capability.TRAINING,
        Capability.TUNING,
        Capability.INFERENCE,
    ]
    Modalities: list[Modality] = [Modality.TEXT]
    Availabilities: list[Availability] = [
        Availability.LOCAL_CPU,
        Availability.LOCAL_GPU,
    ]

    def infer(
        self,
        prompt: str,
        model_path: str,
        huggingface_task: str,
        trust_remote_code: bool = False,
    ) -> str:
        """Infers on a model of the user's choice, using Huggingface pipelines.

        Initializes a pipeline object to infer from using the
        given model and its corresponding task description.

        The trust_remote_code parameter must be set to true if the model
        has custom code that is not part of the transformers library.

        An example of a model that requires trust_remote_code=True is
        MPT-7b-Instruct.
        """
        assert huggingface_task, "Please enter a task description."
        inference_pipeline = pipeline(
            huggingface_task, model=model_path, trust_remote_code=trust_remote_code
        )
        inference_output = inference_pipeline(prompt)
        return inference_output
