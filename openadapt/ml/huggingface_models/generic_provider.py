from base_provider import Availability, CompletionProvider, Modality
from transformers import AutoTokenizer, AutoModel, pipeline


class GenericHuggingFaceProvider(CompletionProvider):
    Name = "Generic HuggingFace Provider"
    Modalities = [Modality.TEXT]
    Availabilities = [Availability.HOSTED]

    def create_tokenizer(model_name: str):
        """
        Fetches and returns the model's corresponding tokenizer object
        from HuggingFace.
        """
        tokenizer = AutoTokenizer.from_pretrained(model_name)
        return tokenizer

    def fetch_model(model_name: str):
        """
        Fetches and returns a model object from HuggingFace
        """
        model = AutoModel.from_pretrained(model_name)
        return model

    def _create_pipeline(model_name: str, task: str, trust_remote_code=False):
        """
        Initializer and returns a pipeline object using the
        given model and its corresponding task description.

        The trust_remote_code parameter must be set to true if the model
        has custom code that is not part of the transformers library.

        An example of a model that requires trust_remote_code=True is
        MPT-7b-Instruct.
        """
        hf_pipeline = pipeline(
            task=task, model=model_name, trust_remote_code=trust_remote_code
        )
        return hf_pipeline

    def infer(prompt: str, model_name: str, task_description: str, use_pipeline=True):
        """
        Infers on the model of the user's choice. Currently resorting to
        defaulting on using HF pipelines for the inference.
        """
        assert use_pipeline and task_description, "Please enter a task description."
        if use_pipeline and task_description:
            inference_pipeline = GenericHuggingFaceProvider._create_pipeline(
                model_name=model_name, task=task_description
            )
            inference_output = inference_pipeline(prompt)
            return inference_output

    def save_model():
        raise NotImplementedError

    def fine_tune():
        raise NotImplementedError
