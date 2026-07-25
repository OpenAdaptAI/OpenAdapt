# openadapt-types

> [!IMPORTANT]
> **Status: Experimental. Interoperability schemas, not the product.** This
> package publishes shared schemas for computer-use agents as an optional
> component, with no production support promise.
>
> The OpenAdapt product is the demonstration compiler,
> [`openadapt-flow`](https://github.com/OpenAdaptAI/openadapt-flow), installed
> via the [`OpenAdapt`](https://github.com/OpenAdaptAI/OpenAdapt) launcher
> (`pip install openadapt`): it compiles a demonstrated GUI workflow into a
> deterministic, locally executable program. Healthy runs make no model calls,
> and it halts instead of guessing when verification fails. Lifecycle labels for
> every repository are in the
> [repository lifecycle registry](https://github.com/OpenAdaptAI/.github/blob/main/REPOSITORY_LIFECYCLE.md).

Canonical Pydantic schemas for computer-use agents.

```
pip install openadapt-types
```

These are the shared `Action` and UI-state types used across the OpenAdapt
stack: recorders emit them, the compiler stores and replays them, the grounding
package resolves their targets, and the privacy package scrubs them. Defining the
schema once keeps every substrate (web, Windows, macOS, Linux, RDP, Citrix/VDI)
on the same contract.

## The OpenAdapt stack

OpenAdapt is a governed demonstration compiler: record a workflow once, compile
the recording into a deterministic program, and replay that program with zero
model calls on the healthy path. When the live screen does not match what was
demonstrated it halts instead of guessing, using identity gates and independent
effect verification. Every substrate is first-class: web and desktop recording
are validated, RDP and Windows replay are early, and Citrix is exploratory.

| Package | Role |
| --- | --- |
| [`openadapt`](https://github.com/OpenAdaptAI/OpenAdapt) | Launcher and installer (`pip install openadapt`) |
| [`openadapt-flow`](https://github.com/OpenAdaptAI/openadapt-flow) | Records, compiles, verifies, and replays workflows |
| [`openadapt-capture`](https://github.com/OpenAdaptAI/openadapt-capture) | Cross-platform local desktop recording |
| **`openadapt-types`** | Canonical action and UI-state schema (this package) |
| [`openadapt-grounding`](https://github.com/OpenAdaptAI/openadapt-grounding) | Local OCR text-anchoring plus optional model grounding |
| [`openadapt-privacy`](https://github.com/OpenAdaptAI/openadapt-privacy) | PHI/PII detection and redaction |

Documentation for the whole stack lives at
[docs.openadapt.ai](https://docs.openadapt.ai).

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
| `ControlOverlayFrameV1` / `ControlOverlayTimelineV1` | PHI-safe execution overlay state bound to exact evidence media |
| `ControlOverlayFrameV2` / `ControlOverlayTimelineV2` | Exact, privacy-safe target geometry for sibling overlays and media composition |

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
# 1. Element-based (preferred, most robust)
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

The same API exports the versioned cross-surface overlay contracts:

```python
from openadapt_types import (
    ControlOverlayFrameV1,
    ControlOverlayFrameV2,
    ControlOverlayTimelineV1,
    ControlOverlayTimelineV2,
)

frame_schema = ControlOverlayFrameV1.model_json_schema()
timeline_schema = ControlOverlayTimelineV1.model_json_schema()
tracking_frame_schema = ControlOverlayFrameV2.model_json_schema()
tracking_timeline_schema = ControlOverlayTimelineV2.model_json_schema()
```

The same schemas ship under `openadapt_types/schemas/` for TypeScript, Rust,
and other consumers. Version 1 remains the control-state contract. Version 2
adds an optional normalized top-level viewport rectangle, the exact source
viewport and DPR, and an exact observation or decoded-media-frame binding
without changing V1.

Overlay schemas reject unknown fields and contain only closed presentation
labels and canonical statuses. Screenshots, action targets, typed values,
identities, URLs, logs, report bodies, and user-authored workflow names remain
outside this public presentation contract.

Target geometry never carries locators, accessible names, values, URLs, or
screenshots. A private live observation uses a run/export-scoped HMAC reference
instead of a linkable raw frame hash. Published media uses the exact media
SHA-256 and decoded frame index. A renderer draws tracking only when that
binding matches; it omits the rectangle rather than replaying selectors,
interpolating movement, or inferring a missing target from adjacent events.
The runtime does not guess a future viewer transform. Desktop, Cloud, and media
renderers map the normalized rectangle through their actual content box.
If multiple runtime states land in one decoded media frame, the producer must
coalesce them deterministically; it must not invent extra media frames or
approximate their timing.

## Design principles

- **Pydantic v2**: runtime validation, JSON Schema export, fast serialization
- **Pixels and structure**: always capture both visual and semantic UI state
- **Node graph**: full element tree, not just the focused element
- **Platform-agnostic**: same schema for web, Windows, macOS, Linux, RDP, Citrix/VDI
- **Extension-friendly**: `raw`, `attributes`, `metadata` fields everywhere
- **Backward compatible**: `_compat` converters for gradual migration

## Dependencies

Just `pydantic>=2.0`. No ML libraries, no heavy deps.

## License

MIT
