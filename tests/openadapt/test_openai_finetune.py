from loguru import logger
import openai
import pytest


@pytest.fixture()
def load_recording() -> list[dict[str, str]]:
    """Loads the Action, Window state pairs."""
    with open(
        "/Users/owaiszahid/Desktop/ff/OpenAdapt/tests/openadapt/assets/recording_fixtures.json",
        "r",
    ) as file:
        incomplete_recording = file.readlines()

    for i in range(len(incomplete_recording)):
        incomplete_recording[i] = eval(incomplete_recording[i])

    return incomplete_recording


def test_failure_finetuned_completion(load_recording: list[dict[str, str]]) -> None:
    """Failure test using OpenAI API call."""

    prompt_str = ""

    for dict in load_recording:
        prompt_str += str(dict["prompt"]) + ","

    test_ft_comp = openai.Completion.create(
        model="davinci:ft-openadaptai-2023-08-18-04-09-43",
        prompt=prompt_str,
        max_tokens=388,
    )

    assert eval(test_ft_comp["choices"]) != load_recording
