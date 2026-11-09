# OpenAdapt ecosystem integration

**Status: current implementation map.** This file replaces the pre-integration
architecture memo that once described `openadapt-capture` as unimplemented and
limited to a prototype. That statement is obsolete.

## Product boundary

`openadapt-flow` is the canonical demonstration compiler and governed runtime.
The OpenAdapt launcher installs it. Focused packages provide optional input,
interop, privacy, and operator surfaces. They do not replace Flow's internal
evidence-rich workflow model.

| Component | Current Flow integration | Qualification and admission boundary |
| --- | --- | --- |
| `openadapt-capture` | Supported native capture input through `openadapt-flow[capture]` and `openadapt_flow.adapters.capture` | Capture's exact product state comes from its active release admission. Each native or remote workflow still needs application-specific qualification. |
| `openadapt-types` | Optional interop boundary through `openadapt-flow[interop]` | The shared schema does not replace Flow's compiled IR or safety contracts. |
| `openadapt-privacy` | Optional source and artifact privacy controls through `openadapt-flow[privacy]` | A recording never becomes safe to upload only because it compiled. |
| `openadapt-grounding` | Optional grounding rung | It cannot authorize an action or prove an effect. |
| `openadapt-verifier` | No runtime integration | It is an unrelated research package and is not required by the product. |

## Capture integration

`openadapt-capture` is the canonical native screen, mouse, keyboard, timing,
window-scope, and media-capture component. Flow does not implement a second
native recorder.

The adapter uses Capture's public API:

```python
session = CaptureSession.load(capture_dir)
actions = session.actions(include_moves=False)
frame = session.get_frame_at(timestamp)
```

It does not read Capture's private database schema. The adapter normalizes the
public actions and frames into Flow's recording contract. It preserves click,
double-click, drag, type, key, shortcut, and scroll semantics. It rejects an
unsupported action instead of dropping it.

The required CI suite tests the released package API, timestamp and frame
alignment, coordinate scaling, secret exclusion, structural observations,
action vocabulary, and all desktop backend selectors. See
[`tests/test_capture_adapter.py`](../tests/test_capture_adapter.py) and
[`openadapt_flow/adapters/capture.py`](../openadapt_flow/adapters/capture.py).

Install the native recording path with:

```bash
python -m pip install 'openadapt-flow[capture]'
```

The Capture dependency remains outside the browser and replay hot paths. A
native recording needs an interactive desktop plus the applicable operating
system permissions. An offline pixel recording cannot reconstruct structural
accessibility evidence. A workflow that needs UIA, Accessibility, or AT-SPI
identity must retain a live structural observation or receive that evidence
during qualification.

For RDP and Citrix, Capture observes the local client window. It does not claim
that a native accessibility tree crosses the remote boundary. Flow uses the
retained pixels, OCR, relational anchors, identity regions, and fresh-frame
checks for that external surface.

## Types integration

Flow's compiled `Step` contains anchors, identity requirements,
postconditions, action risk, effect contracts, and audit state. A shared
`openadapt-types` action describes portable action intent. These are different
layers.

The optional interop module converts supported actions at package boundaries.
Flow keeps its internal IR as the source of truth for compilation and runtime
safety. Consumers must negotiate a schema version. Dependency presence alone
does not upgrade an existing peer contract.

## Privacy integration

Privacy controls run at source-time and artifact boundaries. Secret browser
fields are excluded before an event or frame persists. A sanitized derivative
is created from a copy, inventories every file, binds review to the exact
bytes, and preserves the original inside its trusted boundary.

Live runtime observations can contain sensitive data again. They remain inside
the declared execution boundary.

## Non-product research packages

`openadapt-verifier`, `openadapt-grounding`, `openadapt-retrieval`, and
`openadapt-viewer` are not required to record, compile, replay, or verify a
workflow. Keep a package integration only when a live product boundary consumes
it. Do not infer product state from a package name or from code presence.
