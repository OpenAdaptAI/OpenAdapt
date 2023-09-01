from loguru import logger
import openai

def test_failure_finetuned_completion():
    with open("/Users/owaiszahid/Desktop/ff/OpenAdapt/tests/openadapt/assets/recording_fixtures.json","r") as file:

        incomplete_recording = file.readlines()

    for i in range(len(incomplete_recording)):
        incomplete_recording[i] = eval(incomplete_recording[i])

    prompt_str = ""

    for dict in incomplete_recording:
        prompt_str += str(dict["prompt"]) + ","

    
    test_ft_comp = openai.Completion.create(
        model="davinci:ft-openadaptai-2023-08-18-04-09-43", 
        prompt = prompt_str,
        max_tokens=388
    )

    assert (eval(test_ft_comp["choices"]) != incomplete_recording)

