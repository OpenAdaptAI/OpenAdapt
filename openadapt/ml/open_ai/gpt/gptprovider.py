from openadapt.ml.base_provider import CompletionProvider, Modality, Capability, Availability
import openai


class GPTCompletionProvider(CompletionProvider):
    Name: str = "GPTCompletionProvider"
    Capabilities: list[Capability] = [
        Capability.TRAINING,
        Capability.TUNING,
        Capability.INFERENCE,
    ]
    Modalities: list[Modality] = [Modality.TEXT]
    Availabilities: list[Availability] = [Availability.HOSTED]

    def finetune(prompt_completion_pair: list[dict[str]]):
        # waiting on FineTuning PR which is currently
        # a work in progress.
        raise NotImplementedError

    def infer(gpt_model_name: str, prompt: str):
        """
        Users can only infer from models available within their organization
        on OpenAI. This includes base models provided by OpenAI as well as 
        finetuned models. 
        """
        valid_inference_models = openai.Model.list()["data"]
        for model_dict in valid_inference_models:
            if model_dict["id"] == gpt_model_name:
                if gpt_model_name == "gpt-4" or gpt_model_name == "gpt-3.5-turbo":
                    completion_agent = openai.ChatCompletion.create(
                        model=gpt_model_name,
                        messages=[
                            {
                                "role": "system",
                                "content": "You are a helpful assistant",
                            },
                            {"role": "user", "content": prompt},
                        ],
                    )
                    return completion_agent["choices"][0]["message"]["content"]

                else:
                    completion_agent = openai.Completion.create(
                        model=gpt_model_name, prompt=prompt
                    )
                    return completion_agent["choices"][0]["text"]
