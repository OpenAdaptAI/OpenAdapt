from loguru import logger
from openadapt.ml.fine_tuning.openai.openai_finetune import *
import openai


def test_openai_finetune_on_recording():
    """
    A test to confirm whether the openai finetuning workflow
    works properly on a given recording.

    Please ensure that you have at least ONE recording completed
    using OpenAdapt before running this test.
    """

    davinci_fine_tuner = OpenAIFineTuner("davinci")

    file_path = davinci_fine_tuner.prepare_data_for_tuning(2)

    check_data_output = davinci_fine_tuner.check_data_for_tuning(file_path)

    logger.debug(f"{check_data_output}")

    tune_davinci = davinci_fine_tuner.tune_model(file_path)

    logger.debug(f"{tune_davinci}")


def test_finetuned_completion():
    incomplete_recording = [
        {
            "prompt": "({'name': 'move', 'mouse_x': 314.9765625, 'mouse_y': 391.67578125, 'element_state': {}}, {'title': 'Terminal openadapt \u2014 poetry shell \u25b8 Python \u2014 124\u00d740', 'left': 283, 'top': 109, 'width': 878, 'height': 595, 'window_id': 1129})",
            "completion": " ({'name': 'move', 'mouse_x': 314.9765625, 'mouse_y': 392.61328125, 'element_state': {}}, {'title': 'Terminal openadapt \u2014 poetry shell \u25b8 Python \u2014 124\u00d740', 'left': 283, 'top': 109, 'width': 878, 'height': 595, 'window_id': 1129})",
        },
        {
            "prompt": "({'name': 'move', 'mouse_x': 314.9765625, 'mouse_y': 392.61328125, 'element_state': {}}, {'title': 'Terminal openadapt \u2014 poetry shell \u25b8 Python \u2014 124\u00d740', 'left': 283, 'top': 109, 'width': 878, 'height': 595, 'window_id': 1129})",
            "completion": " ({'name': 'move', 'mouse_x': 314.9765625, 'mouse_y': 393.55078125, 'element_state': {}}, {'title': 'Terminal openadapt \u2014 poetry shell \u25b8 Python \u2014 124\u00d740', 'left': 283, 'top': 109, 'width': 878, 'height': 595, 'window_id': 1129})",
        },
        {
            "prompt": "({'name': 'move', 'mouse_x': 314.9765625, 'mouse_y': 393.55078125, 'element_state': {}}, {'title': 'Terminal openadapt \u2014 poetry shell \u25b8 Python \u2014 124\u00d740', 'left': 283, 'top': 109, 'width': 878, 'height': 595, 'window_id': 1129})",
            "completion": " ({'name': 'move', 'mouse_x': 314.9765625, 'mouse_y': 393.80859375, 'element_state': {}}, {'title': 'Terminal openadapt \u2014 poetry shell \u25b8 Python \u2014 124\u00d740', 'left': 283, 'top': 109, 'width': 878, 'height': 595, 'window_id': 1129})",
        },
        {
            "prompt": "({'name': 'move', 'mouse_x': 314.9765625, 'mouse_y': 393.80859375, 'element_state': {}}, {'title': 'Terminal openadapt \u2014 poetry shell \u25b8 Python \u2014 124\u00d740', 'left': 283, 'top': 109, 'width': 878, 'height': 595, 'window_id': 1129})",
            "completion": " ({'name': 'move', 'mouse_x': 314.9765625, 'mouse_y': 394.06640625, 'element_state': {}}, {'title': 'Terminal openadapt \u2014 poetry shell \u25b8 Python \u2014 124\u00d740', 'left': 283, 'top': 109, 'width': 878, 'height': 595, 'window_id': 1129})",
        },
        {
            "prompt": "({'name': 'move', 'mouse_x': 314.9765625, 'mouse_y': 394.06640625, 'element_state': {}}, {'title': 'Terminal openadapt \u2014 poetry shell \u25b8 Python \u2014 124\u00d740', 'left': 283, 'top': 109, 'width': 878, 'height': 595, 'window_id': 1129})",
            "completion": " ({'name': 'move', 'mouse_x': 314.9765625, 'mouse_y': 392.421875, 'element_state': {}}, {'title': 'Terminal openadapt \u2014 poetry shell \u25b8 Python \u2014 124\u00d740', 'left': 283, 'top': 109, 'width': 878, 'height': 595, 'window_id': 1129})",
        },
        {
            "prompt": "({'name': 'move', 'mouse_x': 314.9765625, 'mouse_y': 392.421875, 'element_state': {}}, {'title': 'Terminal openadapt \u2014 poetry shell \u25b8 Python \u2014 124\u00d740', 'left': 283, 'top': 109, 'width': 878, 'height': 595, 'window_id': 1129})",
            "completion": " ({'name': 'move', 'mouse_x': 314.9765625, 'mouse_y': 387.3359375, 'element_state': {}}, {'title': 'Terminal openadapt \u2014 poetry shell \u25b8 Python \u2014 124\u00d740', 'left': 283, 'top': 109, 'width': 878, 'height': 595, 'window_id': 1129})",
        },
        {
            "prompt": "({'name': 'move', 'mouse_x': 314.9765625, 'mouse_y': 387.3359375, 'element_state': {}}, {'title': 'Terminal openadapt \u2014 poetry shell \u25b8 Python \u2014 124\u00d740', 'left': 283, 'top': 109, 'width': 878, 'height': 595, 'window_id': 1129})",
            "completion": " ({'name': 'move', 'mouse_x': 314.9765625, 'mouse_y': 379.6328125, 'element_state': {}}, {'title': 'Terminal openadapt \u2014 poetry shell \u25b8 Python \u2014 124\u00d740', 'left': 283, 'top': 109, 'width': 878, 'height': 595, 'window_id': 1129})",
        },
        {
            "prompt": "({'name': 'move', 'mouse_x': 314.9765625, 'mouse_y': 379.6328125, 'element_state': {}}, {'title': 'Terminal openadapt \u2014 poetry shell \u25b8 Python \u2014 124\u00d740', 'left': 283, 'top': 109, 'width': 878, 'height': 595, 'window_id': 1129})",
            "completion": " ({'name': 'move', 'mouse_x': 314.9765625, 'mouse_y': 362.47265625, 'element_state': {}}, {'title': 'Terminal openadapt \u2014 poetry shell \u25b8 Python \u2014 124\u00d740', 'left': 283, 'top': 109, 'width': 878, 'height': 595, 'window_id': 1129})",
        },
        {
            "prompt": "({'name': 'move', 'mouse_x': 314.9765625, 'mouse_y': 362.47265625, 'element_state': {}}, {'title': 'Terminal openadapt \u2014 poetry shell \u25b8 Python \u2014 124\u00d740', 'left': 283, 'top': 109, 'width': 878, 'height': 595, 'window_id': 1129})",
            "completion": " ({'name': 'move', 'mouse_x': 314.9765625, 'mouse_y': 345.3125, 'element_state': {}}, {'title': 'Terminal openadapt \u2014 poetry shell \u25b8 Python \u2014 124\u00d740', 'left': 283, 'top': 109, 'width': 878, 'height': 595, 'window_id': 1129})",
        },
        {
            "prompt": "({'name': 'move', 'mouse_x': 314.9765625, 'mouse_y': 345.3125, 'element_state': {}}, {'title': 'Terminal openadapt \u2014 poetry shell \u25b8 Python \u2014 124\u00d740', 'left': 283, 'top': 109, 'width': 878, 'height': 595, 'window_id': 1129})",
            "completion": " ({'name': 'move', 'mouse_x': 314.9765625, 'mouse_y': 323.66796875, 'element_state': {}}, {'title': 'Terminal openadapt \u2014 poetry shell \u25b8 Python \u2014 124\u00d740', 'left': 283, 'top': 109, 'width': 878, 'height': 595, 'window_id': 1129})",
        },
        {
            "prompt": "({'name': 'move', 'mouse_x': 314.9765625, 'mouse_y': 323.66796875, 'element_state': {}}, {'title': 'Terminal openadapt \u2014 poetry shell \u25b8 Python \u2014 124\u00d740', 'left': 283, 'top': 109, 'width': 878, 'height': 595, 'window_id': 1129})",
            "completion": " ({'name': 'move', 'mouse_x': 315.93359375, 'mouse_y': 298.75, 'element_state': {}}, {'title': 'Terminal openadapt \u2014 poetry shell \u25b8 Python \u2014 124\u00d740', 'left': 283, 'top': 109, 'width': 878, 'height': 595, 'window_id': 1129})",
        },
        {
            "prompt": "({'name': 'move', 'mouse_x': 315.93359375, 'mouse_y': 298.75, 'element_state': {}}, {'title': 'Terminal openadapt \u2014 poetry shell \u25b8 Python \u2014 124\u00d740', 'left': 283, 'top': 109, 'width': 878, 'height': 595, 'window_id': 1129})",
            "completion": " ({'name': 'move', 'mouse_x': 319.9921875, 'mouse_y': 270.328125, 'element_state': {}}, {'title': 'Terminal openadapt \u2014 poetry shell \u25b8 Python \u2014 124\u00d740', 'left': 283, 'top': 109, 'width': 878, 'height': 595, 'window_id': 1129})",
        },
        {
            "prompt": "({'name': 'move', 'mouse_x': 319.9921875, 'mouse_y': 270.328125, 'element_state': {}}, {'title': 'Terminal openadapt \u2014 poetry shell \u25b8 Python \u2014 124\u00d740', 'left': 283, 'top': 109, 'width': 878, 'height': 595, 'window_id': 1129})",
            "completion": " ({'name': 'move', 'mouse_x': 324.05078125, 'mouse_y': 241.90625, 'element_state': {}}, {'title': 'Terminal openadapt \u2014 poetry shell \u25b8 Python \u2014 124\u00d740', 'left': 283, 'top': 109, 'width': 878, 'height': 595, 'window_id': 1129})",
        },
        {
            "prompt": "({'name': 'move', 'mouse_x': 324.05078125, 'mouse_y': 241.90625, 'element_state': {}}, {'title': 'Terminal openadapt \u2014 poetry shell \u25b8 Python \u2014 124\u00d740', 'left': 283, 'top': 109, 'width': 878, 'height': 595, 'window_id': 1129})",
            "completion": " ({'name': 'move', 'mouse_x': 330.30859375, 'mouse_y': 211.6484375, 'element_state': {}}, {'title': 'Terminal openadapt \u2014 poetry shell \u25b8 Python \u2014 124\u00d740', 'left': 283, 'top': 109, 'width': 878, 'height': 595, 'window_id': 1129})",
        },
        {
            "prompt": "({'name': 'move', 'mouse_x': 330.30859375, 'mouse_y': 211.6484375, 'element_state': {}}, {'title': 'Terminal openadapt \u2014 poetry shell \u25b8 Python \u2014 124\u00d740', 'left': 283, 'top': 109, 'width': 878, 'height': 595, 'window_id': 1129})",
            "completion": " ({'name': 'move', 'mouse_x': 338.65234375, 'mouse_y': 182.43359375, 'element_state': {}}, {'title': 'Terminal openadapt \u2014 poetry shell \u25b8 Python \u2014 124\u00d740', 'left': 283, 'top': 109, 'width': 878, 'height': 595, 'window_id': 1129})",
        },
        {
            "prompt": "({'name': 'move', 'mouse_x': 338.65234375, 'mouse_y': 182.43359375, 'element_state': {}}, {'title': 'Terminal openadapt \u2014 poetry shell \u25b8 Python \u2014 124\u00d740', 'left': 283, 'top': 109, 'width': 878, 'height': 595, 'window_id': 1129})",
            "completion": " ({'name': 'move', 'mouse_x': 342.03125, 'mouse_y': 171.609375, 'element_state': {}}, {'title': 'Terminal openadapt \u2014 poetry shell \u25b8 Python \u2014 124\u00d740', 'left': 283, 'top': 109, 'width': 878, 'height': 595, 'window_id': 1129})",
        },
        {
            "prompt": "({'name': 'move', 'mouse_x': 342.03125, 'mouse_y': 171.609375, 'element_state': {}}, {'title': 'Terminal openadapt \u2014 poetry shell \u25b8 Python \u2014 124\u00d740', 'left': 283, 'top': 109, 'width': 878, 'height': 595, 'window_id': 1129})",
            "completion": " ({'name': 'move', 'mouse_x': 348.5390625, 'mouse_y': 149.28515625, 'element_state': {}}, {'title': 'Terminal openadapt \u2014 poetry shell \u25b8 Python \u2014 124\u00d740', 'left': 283, 'top': 109, 'width': 878, 'height': 595, 'window_id': 1129})",
        },
        {
            "prompt": "({'name': 'move', 'mouse_x': 348.5390625, 'mouse_y': 149.28515625, 'element_state': {}}, {'title': 'Terminal openadapt \u2014 poetry shell \u25b8 Python \u2014 124\u00d740', 'left': 283, 'top': 109, 'width': 878, 'height': 595, 'window_id': 1129})",
            "completion": " ({'name': 'move', 'mouse_x': 354.65234375, 'mouse_y': 130.06640625, 'element_state': {}}, {'title': 'Terminal openadapt \u2014 poetry shell \u25b8 Python \u2014 124\u00d740', 'left': 283, 'top': 109, 'width': 878, 'height': 595, 'window_id': 1129})",
        },
        {
            "prompt": "({'name': 'move', 'mouse_x': 354.65234375, 'mouse_y': 130.06640625, 'element_state': {}}, {'title': 'Terminal openadapt \u2014 poetry shell \u25b8 Python \u2014 124\u00d740', 'left': 283, 'top': 109, 'width': 878, 'height': 595, 'window_id': 1129})",
            "completion": " ({'name': 'move', 'mouse_x': 356.796875, 'mouse_y': 124.1640625, 'element_state': {}}, {'title': 'Terminal openadapt \u2014 poetry shell \u25b8 Python \u2014 124\u00d740', 'left': 283, 'top': 109, 'width': 878, 'height': 595, 'window_id': 1129})",
        },
    ]

    prompt_str = ""
    for dict in incomplete_recording:
        prompt_str += str(dict["prompt"]) + ","

    
    test_ft_comp = openai.Completion.create(
        model="davinci:ft-openadaptai-2023-08-18-04-09-43", 
        prompt = prompt_str,
        max_tokens=388
    )
    logger.debug(f'{test_ft_comp["choices"]=}')


if __name__ == "__main__":
    test_openai_finetune_on_recording()
