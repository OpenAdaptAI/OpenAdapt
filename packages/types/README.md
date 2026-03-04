# openadapt-types

Canonical Pydantic schemas for computer-use agents.

```
pip install openadapt-types
```

## What's in the box

| Schema | Purpose |
|--------|---------|
| `ComputerState` | Screen state: screenshot + UI element graph + window context |
| `UINode` | Single UI element with role, bbox, hierarchy, platform anchors |
| `Action` | Agent action with typed action space + flexible targeting |
| `ActionTarget` | Where to act: `node_id` > `description` > `(x, y)` coordinates |
| `ActionResult` | Execution outcome with error taxonomy + state delta |
| `Episode` / `Step` | Complete task trajectory (observation → action → result) |
| `FailureRecord` | Classified failure for dataset pipelines |

## Quick start

```python
from openadapt_types import (
    Action, ActionTarget, ActionType,
    ComputerState, UINode, BoundingBox,
)

# Describe what's on screen
state = ComputerState(
    viewport=(1920, 1080),
    nodes=[
        UINode(node_id="n0", role="window", name="My App", children_ids=["n1"]),
        UINode(node_id="n1", role="button", name="Submit", parent_id="n0",
               bbox=BoundingBox(x=500, y=400, width=100, height=40)),
    ],
)

# Agent decides what to do
action = Action(
    type=ActionType.CLICK,
    target=ActionTarget(node_id="n1"),
    reasoning="Click Submit to proceed",
)

# Render element tree for LLM prompts
print(state.to_text_tree())
# [n0] window: My App
#   [n1] button: Submit
```

## Action targeting

`ActionTarget` supports three grounding strategies (in priority order):

```python
# 1. Element-based (preferred — most robust)
ActionTarget(node_id="n1")

# 2. Description-based (resolved by grounding module)
ActionTarget(description="the blue submit button")

# 3. Coordinate-based (fallback)
ActionTarget(x=550, y=420)
ActionTarget(x=0.29, y=0.39, is_normalized=True)
```

Agents SHOULD produce `node_id` or `description`. The runtime resolves to coordinates.

## Compatibility with existing schemas

Converters for three existing OpenAdapt schema formats:

```python
from openadapt_types._compat import (
    from_benchmark_observation,   # openadapt-evals BenchmarkObservation
    from_benchmark_action,        # openadapt-evals BenchmarkAction
    from_ml_observation,          # openadapt-ml Observation
    from_ml_action,               # openadapt-ml Action
    from_omnimcp_screen_state,    # omnimcp ScreenState
    from_omnimcp_action_decision, # omnimcp ActionDecision
)

# Convert existing data
state = from_benchmark_observation(obs.__dict__)
action = from_benchmark_action(act.__dict__)
```

## JSON Schema

Export for language-agnostic tooling:

```python
import json
from openadapt_types import ComputerState, Action, Episode

# Get JSON Schema
schema = ComputerState.model_json_schema()
print(json.dumps(schema, indent=2))
```

## Design principles

- **Pydantic v2** — runtime validation, JSON Schema export, fast serialization
- **Pixels + structure** — always capture both visual and semantic UI state
- **Node graph** — full element tree, not just focused element
- **Platform-agnostic** — same schema for Windows, macOS, Linux, web
- **Extension-friendly** — `raw`, `attributes`, `metadata` fields everywhere
- **Backward compatible** — `_compat` converters for gradual migration

## Dependencies

Just `pydantic>=2.0`. No ML libraries, no heavy deps.

## License

MIT
