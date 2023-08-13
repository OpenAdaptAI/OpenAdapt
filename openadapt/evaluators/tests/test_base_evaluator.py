from openadapt.evaluators.evaluator import BaseEvaluator

LOG_LEVEL = "DEBUG"
MAX_SCREEN_SIZE = (1920, 1080)
MAX_INPUT_SIZE = 1024
MAX_TOKENS = 1024


base_evaluator = BaseEvaluator(
    model_name="gpt2",
)
base_evaluator.init_model = lambda: None


def test_score_wrong_completion():
    # Test that the score is False when the completion is wrong

    # mock that base_evaluator.build_prompt returns a prompt
    base_evaluator.build_prompt = (
        lambda ref_window, action, active_window: "some prompt"
    )
    base_evaluator.get_completion = lambda prompt: "wrong completion"
    assert base_evaluator.evaluate() == False


def test_score_wrong_action_type():
    base_evaluator.build_prompt = (
        lambda ref_window, action, active_window: "some prompt"
    )
    base_evaluator.get_completion = (
        lambda prompt: "[{'name': 'press', 'key': 'a', 'element_state': {}}]"
    )
    score = base_evaluator.evaluate()
    assert score is False
