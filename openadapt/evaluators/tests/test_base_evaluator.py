import math

from openadapt.evaluators.data_models import KeyAction, MouseAction, Window
from openadapt.evaluators.evaluator import BaseEvaluator

LOG_LEVEL = "DEBUG"
MAX_SCREEN_SIZE = (1920, 1080)
MAX_INPUT_SIZE = 1024
MAX_TOKENS = 1024


base_evaluator = BaseEvaluator(
    model_name="gpt2",
)


def test_score_wrong_completion():
    # Test that the score is 0 when the completion is wrong

    # mock that base_evaluator.build_prompt returns a prompt
    base_evaluator.build_prompt = (
        lambda ref_window, action, active_window: "some prompt"
    )
    base_evaluator.get_completion = lambda prompt: "wrong completion"
    assert base_evaluator.evaluate_single_action() == 0


def test_score_correct_completion():
    base_evaluator.build_prompt = (
        lambda ref_window, action, active_window: "some prompt"
    )
    base_evaluator.get_completion = (
        lambda prompt: (
            "[{'name': 'click', 'mouse_x': 25.0, 'mouse_y': 55.0, 'mouse_button_name':"
            " 'left', 'mouse_pressed': True, 'element_state': {}}]"
        )
    )
    score = base_evaluator.evaluate_single_action()
    # assert score is approximately 0.9
    assert math.isclose(score, 0.9, abs_tol=0.1)


def test_score_correct_completion_with_close_action():
    base_evaluator.build_prompt = (
        lambda ref_window, action, active_window: "some prompt"
    )
    base_evaluator.get_completion = (
        lambda prompt: (
            "[{'name': 'move', 'mouse_x': 25.0, 'mouse_y': 55.0, 'mouse_button_name':"
            " 'right', 'mouse_pressed': True, 'element_state': {}}]"
        )
    )
    score = base_evaluator.evaluate_single_action()
    # assert score is approximately 0.9
    assert math.isclose(score, 0.7, abs_tol=0.1)


def test_score_wrong_action_type():
    base_evaluator.build_prompt = (
        lambda ref_window, action, active_window: "some prompt"
    )
    base_evaluator.get_completion = (
        lambda prompt: "[{'name': 'press', 'key': 'a', 'element_state': {}}]"
    )
    score = base_evaluator.evaluate_single_action()
    assert math.isclose(score, 0.1, abs_tol=0.05)
