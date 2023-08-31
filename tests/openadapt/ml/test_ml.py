"""Module to test the CompletionProvider API."""
from openai.error import AuthenticationError
import pytest

from openadapt.ml.base_provider import CompletionProviderFactory, Modality
from openadapt.ml.huggingface.generic_provider import LocalHuggingFaceProvider
from openadapt.ml.open_ai.gpt.gptprovider import GPTCompletionProvider


@pytest.fixture()
def gpt_provider() -> GPTCompletionProvider:
    """Fixture for GPTCompletionProvider."""
    test_gpt_provider = GPTCompletionProvider()
    return test_gpt_provider


@pytest.fixture()
def hf_provider() -> LocalHuggingFaceProvider:
    """Fixture for LocalHuggingFaceProvider."""
    test_hf_provider = LocalHuggingFaceProvider()
    return test_hf_provider


def test_openai_completion_provider(gpt_provider: gpt_provider) -> None:
    """Openai Completion Provider test."""
    try:
        gpt_4_chat_response = gpt_provider.infer(
            "gpt-4", "What is your maximum context size?"
        )

        gpt_3_turbo_chat_response = gpt_provider.infer(
            "gpt-3.5-turbo", "What day is it today?"
        )

        davinci_completion_response = gpt_provider.infer("davinci", "Today is ")

        assert (
            len(gpt_3_turbo_chat_response) > 0
            and len(gpt_4_chat_response) > 0
            and len(davinci_completion_response) > 0
        )
    except AuthenticationError:
        exception_msg = (
            "Incorrect API key provided: <set you**************env>. You can find your"
            " API key at https://platform.openai.com/account/api-keys."
        )
        pytest.mark.skip(reason=exception_msg)


def test_huggingface_completion_provider(hf_provider: hf_provider) -> None:
    """Huggingface Completion Provider test."""
    inference_output = hf_provider.infer(
        "What is the next number in the series 1, 3, 5, 7 ",
        "gpt2-medium",
        "text-generation",
        trust_remote_code=True,
    )

    assert len(inference_output) > 0


def test_abstract_completion_provider_factory() -> None:
    """Completion Provider Factory test."""
    test_modality = Modality.TEXT
    providers_list = CompletionProviderFactory.get_for_modality(test_modality)

    assert providers_list, (test_modality, providers_list)

    for completion_providers in providers_list:
        assert test_modality in completion_providers.Modalities
