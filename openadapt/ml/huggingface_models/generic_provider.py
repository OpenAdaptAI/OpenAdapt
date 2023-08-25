from openadapt.ml.base_provider import (
    Availability,
    Capability,
    CompletionProvider,
    Modality,
)
from transformers import pipeline


class GenericHuggingFaceProvider(CompletionProvider):
    """GenericHuggingFaceProvider class."""

    Name: str = "Generic HuggingFace Provider"
    Capabilities: list[Capability] = [
        Capability.TRAINING,
        Capability.TUNING,
        Capability.INFERENCE,
    ]
    Modalities: list[Modality] = [Modality.TEXT]
    Availabilities: list[Availability] = [Availability.HOSTED]

    def infer(
        self,
        prompt: str,
        model_path: str,
        task_description: str,
        use_pipeline=True,
        trust_remote_code=False,
    ):
        """
        Infers on a model of the user's choice, using Huggingface pipelines.

        Initializes a pipeline object to infer from using the
        given model and its corresponding task description.

        The trust_remote_code parameter must be set to true if the model
        has custom code that is not part of the transformers library.

        An example of a model that requires trust_remote_code=True is
        MPT-7b-Instruct.
        """
        assert use_pipeline and task_description, "Please enter a task description."
        inference_pipeline = pipeline(
            task_description, model=model_path, trust_remote_code=trust_remote_code
        )
        inference_output = inference_pipeline(prompt)
        return inference_output
