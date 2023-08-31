"""GPT Completion Provider file."""

import openai

from openadapt.ml.base_provider import (
    Availability,
    Capability,
    CompletionProvider,
    Modality,
)

CHAT_MODEL_NAMES = {"gpt-4", "gpt-3.5-turbo"}


class GPTCompletionProvider(CompletionProvider):
    """GPTCompletionProvider class."""

    Name: str = "GPTCompletionProvider"
    Capabilities: list[Capability] = [
        Capability.TRAINING,
        Capability.TUNING,
        Capability.INFERENCE,
    ]
    Modalities: list[Modality] = [Modality.TEXT]
    Availabilities: list[Availability] = [Availability.HOSTED]

    def finetune(self, prompt_completion_pair: list[dict[str]]) -> None:
        """Fine-tune the GPT model."""
        # TODO: implement once fine tuning is merged
        raise NotImplementedError

    def infer(self, gpt_model_name: str, prompt: str) -> str:
        """Users can only infer from models available within their OpenAI organization.

        This includes base models provided by OpenAI as well as
        finetuned models.
        """
        valid_inference_models = openai.Model.list()["data"]
        for model_dict in valid_inference_models:
            if model_dict["id"] == gpt_model_name:
                if gpt_model_name in CHAT_MODEL_NAMES:
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
