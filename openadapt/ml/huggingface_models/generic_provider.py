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

    def create_pipeline(model_name: str, task: str, trust_remote_code=False):
        """
        Initializer and returns a pipeline object using the
        given model and its corresponding task description.
        """
        hf_pipeline = pipeline(
            task=task, model=model_name, trust_remote_code=trust_remote_code
        )
        return hf_pipeline
