# openadapt-types

[![Packages CI](https://github.com/OpenAdaptAI/OpenAdapt/actions/workflows/packages.yml/badge.svg)](https://github.com/OpenAdaptAI/OpenAdapt/actions/workflows/packages.yml)
[![PyPI](https://img.shields.io/pypi/v/openadapt-types)](https://pypi.org/project/openadapt-types/)
[![Python](https://img.shields.io/pypi/pyversions/openadapt-types)](https://pypi.org/project/openadapt-types/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](https://github.com/OpenAdaptAI/OpenAdapt/blob/main/packages/types/LICENSE)

Pydantic models for computer-use agents: what is on the screen, what to do
to it, and what happened when you did. Use them when you build an agent, a
screen recorder, or a benchmark harness and want one validated format for
observations and actions, plus JSON Schema for code that isn't in Python.

The release on PyPI (0.22.0) needs Python 3.10 or later, and its only
dependency is Pydantic 2. On `main`, the models live in Flow as
`openadapt_flow.contracts`, and this package keeps `import openadapt_types`
working on top of it. Releases built from `main` depend on `openadapt-flow`
and need Python 3.10 to 3.12.

## Quick start

```bash
pip install openadapt-types
```

```python
from openadapt_types import (
    Action, ActionTarget, ActionType, BoundingBox, ComputerState, UINode,
)

# What is on the screen
state = ComputerState(
    viewport=(1920, 1080),
    nodes=[
        UINode(node_id="n0", role="window", name="My App", children_ids=["n1"]),
        UINode(node_id="n1", role="button", name="Submit", parent_id="n0",
               bbox=BoundingBox(x=500, y=400, width=100, height=40)),
    ],
)
print(state.to_text_tree())
# [n0] window: My App
#   [n1] button: Submit

# What to do to it
action = Action(type=ActionType.CLICK, target=ActionTarget(node_id="n1"))
print(action.model_dump_json(exclude_none=True))
# {"type":"click","target":{"node_id":"n1","is_normalized":false}}
```

`to_text_tree()` returns the element tree as text you can put straight into a
prompt. Every model checks its fields when you create it, round-trips through
JSON, and gives you a JSON Schema from `model_json_schema()`.

OpenAdapt's benchmark harness
([openadapt-evals](https://github.com/OpenAdaptAI/openadapt-evals)) and
training code ([openadapt-ml](https://github.com/OpenAdaptAI/openadapt-ml))
use these types. If you want to show a task and have it run again on its
own, install [OpenAdapt](https://github.com/OpenAdaptAI/OpenAdapt)
(`pip install openadapt`) instead.

## What's in it

| Type | What it holds |
|---|---|
| `ComputerState` | One observation: screenshot, the full UI element tree, windows, URL, and platform |
| `UINode` | One element: role, name, value, bounding box, parent and children, and platform anchors such as a Windows AutomationId, an XPath, or a CSS selector |
| `Action`, `ActionTarget` | What to do (click, type, press a key, scroll, drag, open an app, and more) and where |
| `ActionResult` | Whether the action ran, an error category, and which elements changed |
| `Episode`, `Step` | A whole trajectory of observations, actions, results, and rewards, saved to and loaded from JSON |
| `FailureRecord` | A failure sorted into a category such as grounding, navigation, or execution, for dataset pipelines |
| `PlainOutcomeV1` | An OpenAdapt run's result in plain words, whether anything may have changed, and whether a retry is safe |

The same screen model covers web, Windows, macOS, and Linux. It keeps the
screenshot and the element tree together, because each holds information the
other lacks. `raw`, `attributes`, and `metadata` fields let you carry your own
data through without forking the schema.

## Targeting

`ActionTarget` accepts three kinds of answer to "which element". Prefer them
in this order:

```python
from openadapt_types import ActionTarget

ActionTarget(node_id="n1")                          # an element in the tree
ActionTarget(description="the blue submit button")  # resolved later by a grounding model
ActionTarget(x=550, y=420)                          # pixel coordinates, last resort
ActionTarget(x=0.29, y=0.39, is_normalized=True)    # coordinates as a fraction of the screen
```

Have your agent produce a `node_id` or a `description` and work out the pixels
when the action runs. Coordinates break when a window moves.

## Read an action from a model's reply

```python
from openadapt_types import parse_action

action = parse_action("Thought: the form is complete.\nAction: CLICK(x=0.29, y=0.39)")
print(action.type.value, action.target.x, action.target.y)  # click 0.29 0.39
print(action.reasoning)                                     # the form is complete.

print(parse_action("I'm not sure what to do").type.value)  # fail
```

`parse_action()` reads the `NAME(arg=value)` form shown here and JSON,
including JSON inside a code fence. When `x` and `y` are both between 0 and
1, it reads them as fractions of the screen; otherwise it reads them as
pixels. A reply it can't read becomes a `fail` action with the reason in
`reasoning`, and the parser logs a warning. A harness can't mistake an
unreadable reply for a finished task.

## Report how a run ended

`plain_outcome()` turns the status values from an OpenAdapt run into one
result a person can act on:

```python
from openadapt_types import plain_outcome

result = plain_outcome(
    transaction_outcome="RECONCILIATION_REQUIRED",
    execution_outcome="HALTED",
)
print(result.label)          # Check the record
print(result.next_action)    # A save may have gone through. Check the record before anyone runs this again.
print(result.safe_to_retry)  # False
```

An unknown or contradictory status reads "Check the record", never "nothing
was written". The table that maps status values to results also ships as
JSON for other languages, and the `openadapt-vocab-lint` command checks
user-facing text against it. See
[docs/VOCABULARY.md](https://github.com/OpenAdaptAI/OpenAdapt/blob/main/packages/types/docs/VOCABULARY.md).

## JSON Schema for other languages

```python
import json
from openadapt_types import ComputerState

print(json.dumps(ComputerState.model_json_schema(), indent=2))
```

The messages that OpenAdapt components exchange ship under
`openadapt_types/schemas/`: forty-four files, including `execute-v1-openapi.json`,
the OpenAPI document for OpenAdapt Execute, the API that starts runs and
reports their results. Two files hold test vectors, and the rest are JSON
Schema files.
[docs/CONTRACTS.md](https://github.com/OpenAdaptAI/OpenAdapt/blob/main/packages/types/docs/CONTRACTS.md)
lists each contract, its Python model, and what it may carry.

## Convert from older formats

```python
from openadapt_types import from_benchmark_action

action = from_benchmark_action({"type": "click", "x": 0.5, "y": 0.3})
print(action.type.value, action.target.is_normalized)          # click True
print(from_benchmark_action({"type": "teleport"}).type.value)  # fail
```

`from_benchmark_action()` reads an openadapt-evals action dict. The other
converters are in `openadapt_types._compat`: `from_benchmark_observation()`
for openadapt-evals observations, `from_ml_*` for openadapt-ml, and
`from_omnimcp_*` for omnimcp. Each converter takes a plain dict, such as
`obs.__dict__`. An action type it doesn't recognize becomes a `fail` action,
never `done`.

## Limits

- The API can still change between minor versions before 1.0. Pin the minor
  version you test against.
- `ComputerState.screenshot_png` holds raw bytes, which don't convert to
  JSON: `model_dump_json()` and `Episode.to_json()` raise an error. To save a
  state or an episode as JSON, put the screenshot in `screenshot_path` or
  `screenshot_base64` instead.
- In the `NAME(arg=value)` form, `parse_action()` reads only `x` and `y` as a
  target. `CLICK(node_id="n1")` returns a click with no target. Use the JSON
  form for `node_id` and `description` targets.
- Some rules compare one field with another, such as "a result that says
  nothing changed can't also say something may have changed". The Python
  models enforce them. The JSON Schema, from the files or from
  `model_json_schema()`, doesn't always, so code in another language has to
  check them itself.

## License

[MIT](https://github.com/OpenAdaptAI/OpenAdapt/blob/main/packages/types/LICENSE)
