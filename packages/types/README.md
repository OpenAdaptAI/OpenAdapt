# openadapt-types

Pydantic schemas for describing a screen and an action on it. Recorders emit
them, the compiler stores and replays them, the grounding package resolves
their targets, and the privacy package scrubs them. One definition, so nothing
in the stack has to translate.

```bash
pip install openadapt-types
```

Optional, and not the product. If you want to record and replay a workflow, you
want [`openadapt-flow`](https://github.com/OpenAdaptAI/openadapt-flow) instead;
this is the contract underneath it, published separately for anyone building
their own computer-use agent. The API is not stable across minor versions yet.

## Describe a screen, then act on it

```python
from openadapt_types import (
    Action, ActionTarget, ActionType,
    ComputerState, UINode, BoundingBox,
)

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

action = Action(
    type=ActionType.CLICK,
    target=ActionTarget(node_id="n1"),
    reasoning="Click Submit to proceed",
)
```

`to_text_tree()` exists so you can drop the element tree straight into a prompt.

## Targeting

`ActionTarget` takes three kinds of answer to "which thing", and the runtime
prefers them in this order:

```python
ActionTarget(node_id="n1")                          # an element the recorder saw
ActionTarget(description="the blue submit button")  # resolved by the grounder
ActionTarget(x=550, y=420)                          # coordinates, last resort
ActionTarget(x=0.29, y=0.39, is_normalized=True)
```

An agent should produce a `node_id` or a `description` and let the runtime work
out the pixels. Coordinates are the thing that breaks when a window moves.

## The rest of the types

| Type | What it holds |
|---|---|
| `ComputerState` | Screenshot, UI element graph, window context |
| `UINode` | One element: role, bbox, hierarchy, platform anchors |
| `Action` | A typed action plus its target |
| `ActionResult` | The outcome, with an error taxonomy and a state delta |
| `Episode` / `Step` | A whole trajectory: observation, action, result |
| `FailureRecord` | A classified failure, for dataset pipelines |
| `OracleObservation` | One independent effect read. Production `VERIFIED` needs tier 2 or 3 |
| `ArtifactRefV1` | A path-free reference to an immutable process artifact |
| `CodeCapabilityManifestV1` | Exact Python, locked dependencies, typed I/O, permissions, and verifier bindings |
| `ProcessEvidenceReceiptV1` | One signed root over child receipts, human receipts, and the artifact graph |
| `AuthenticationTaskContractV1` | A value-free login requirement bound to an existing attended task |
| `AuthoringObserveV1` | PHI-safe authoring observe tree for the hosted MCP wire |
| `AuthoringCommandV1` | Mailbox envelope. Hosted click is `node_id` only; compile is `needs_human_admit` |
| `AuthoringBindV1` | Bind status plus exact `oab_` / `oals_` parsers. No tree, tokens, or secrets |

Plus the versioned wire contracts: `ControlOverlayFrameV1`/`V2` and
`ControlOverlayTimelineV1`/`V2` for PHI-safe execution overlays,
`ExecuteRequestV1` / `ExecuteStatusV1` / `ExecuteEvidenceReceiptV1` for
asynchronous qualified execution, `EffectStrengthV1`, and the
`BusinessDecision*V1` family for signed, finite human choices. What those
contracts may and may not carry is in
[docs/CONTRACTS.md](docs/CONTRACTS.md). Oracle tiers and the ten-line
adapter are in [docs/ORACLE.md](docs/ORACLE.md). Code capabilities and process
artifacts are in [docs/PROCESS_CAPABILITIES.md](docs/PROCESS_CAPABILITIES.md).

## JSON Schema for everything else

```python
import json
from openadapt_types import ComputerState

print(json.dumps(ComputerState.model_json_schema(), indent=2))
```

The same schemas ship as JSON under `openadapt_types/schemas/` for TypeScript,
Rust, and anything else that isn't Python. Twenty-seven files, including
`execute-v1-openapi.json`, the public OpenAdapt Execute contract.

## Converting from the older formats

```python
from openadapt_types._compat import (
    from_benchmark_observation,   # openadapt-evals BenchmarkObservation
    from_benchmark_action,        # openadapt-evals BenchmarkAction
    from_ml_observation,          # openadapt-ml Observation
    from_ml_action,               # openadapt-ml Action
    from_omnimcp_screen_state,    # omnimcp ScreenState
    from_omnimcp_action_decision, # omnimcp ActionDecision
)

state = from_benchmark_observation(obs.__dict__)
```

## OpenAdapt Execute

A partner sends an authorized request, gets an execution ID back, and then
either reads a terminal receipt or waits for a signed webhook. The contract
exposes no runner, no customer data, no evidence bytes, and no control-plane
internals. A `verified` receipt needs an oracle at tier 2 or 3 (API, DB,
file, ack, or a counterparty artifact). Visual and OCR reads are tier 0
and cannot mint it.

```python
from openadapt_types import ExecuteClient

client = ExecuteClient(
    base_url="https://app.openadapt.ai/api",
    bearer_token="partner-provisioned-token",
)
```

The client requires HTTPS and refuses to follow a redirect before it would send
the bearer token somewhere else. It also doesn't poll forever, deliberately: a
workflow can sit waiting for a human decision or a reconciliation, so treat the
terminal receipt or the signed webhook as the completion signal, not a timeout.

Reference Python and TypeScript clients: [`examples/execute`](examples/execute/).

## Design

Pydantic v2, so you get runtime validation, JSON Schema export, and fast
serialization. Pixels and structure are both captured, always, because either
one alone loses information the other has. The node graph is the full element
tree rather than the focused element. The same schema covers web, Windows,
macOS, Linux, RDP, and Citrix/VDI. `raw`, `attributes`, and `metadata` fields
exist everywhere so you can carry your own data through without forking.

The only dependency is `pydantic>=2.0`. No ML libraries.

## License

[MIT](LICENSE)
