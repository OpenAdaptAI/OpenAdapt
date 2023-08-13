### How does the evaluator work ?

The `BaseEvaluator` class perform the following action:

- For a given model, generate a single action (either mouse action or key press action)
- Give a single window reference and action reference, run the completion to get the predicted action.
- Evaluate the predicted action with the refrence action.


The `BaseEvaluator` class does NOT implement code related to model prompts and completion. It only peform the evaluation based on a basic single mouse action or key board action.

The score is given as following:
For any given pair of `active_window` and `reference_action`:

- If both the predicted action and reference action are of type key press and the same key is press, give score `True`
- If both the predicted action and reference action are of type mouse click or mouse movement and the clicked, moved position of the mouse cursor is within the boundary of the active window, give score `True`
- Give score `False` to everything else


### How to use the `evaluator` module

1. For generation of simple action fixtures:

```python
from openadapt.evaluators import fixtures
ref_window, action, active_window = fixtures.generate_single_mouse()
ref_window, action, active_window = fixtures.generate_multi_click()
ref_window, action, active_window = fixtures.generate_multi_action_sequence()
```

2. For evaluation of a model

Refer to `examples` for a simple examples. In order to evaluate, for example, a `fine-tuned-model`, we need to add the a class which inherits from `BaseEvaluator`
and implement the following methods

- `init_model`: how to init model and tokenizer
- `get_completion`: how to get completion from model
- `build_prompt`: how to build the prompt
- `parse_completion`: how to parse a completion to a valid Action


As these methods are model specific, it is not implemented inside `BaseEvaluator`. Refer to [an example for implementation](./examples/gpt2_evaluator.py)
