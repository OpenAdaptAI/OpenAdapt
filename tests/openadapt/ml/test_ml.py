from openadapt.ml.completion_factory import CompletionProviderFactory
from openadapt.ml.base_provider import Modality
from openadapt.ml.open_ai.gpt.gptprovider import GPTCompletionProvider
from openadapt.ml.huggingface_models.generic_provider import GenericHuggingFaceProvider
import openai


def test_openai_completion_provider():
    gpt_4_chat_response = GPTCompletionProvider.infer(
        "gpt-4", "What is your maximum context size?"
    )

    gpt_3_turbo_chat_response = GPTCompletionProvider.infer(
        "gpt-3.5-turbo", "What day is it today?"
    )

    davinci_completion_response = GPTCompletionProvider.infer("davinci", "Today is ")

    assert (
        len(gpt_3_turbo_chat_response) > 0
        and len(gpt_4_chat_response) > 0
        and len(davinci_completion_response) > 0
    )


def test_huggingface_completion_provider():
    inference_output = GenericHuggingFaceProvider.infer(
        "What is the next number in the series 1, 3, 5, 7 ",
        "gpt2-medium",
        "text-generation",
        trust_remote_code=True,
    )

    assert len(inference_output) > 0


def test_abstract_completion_provider_factory():
    test_modality = Modality.TEXT
    output = CompletionProviderFactory.get_for_modality(test_modality)

    assert output
    print(output)
    assert test_modality in output


if __name__ == "__main__":
    test_abstract_completion_provider_factory()
