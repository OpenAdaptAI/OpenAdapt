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

    # TODO: Pipeline is a hack, give users the option to decode logits
    def infer(prompt: str, model_name: str, task_description=True, use_pipeline=True):
        """
        Infers on the model of the user's choice. Currently resorting to
        defaulting on using HF pipelines for the inference.
        """
        if use_pipeline and task_description:
            inference_pipeline = GenericHuggingFaceProvider.create_pipeline(
                model_name=model_name, task=task_description
            )
            inference_output = inference_pipeline(prompt)
            return inference_output
        else:
            tokenizer = GenericHuggingFaceProvider.create_tokenizer(
                model_name=model_name
            )
            model = GenericHuggingFaceProvider.fetch_model(model_name=model_name)
            raise NotImplementedError
