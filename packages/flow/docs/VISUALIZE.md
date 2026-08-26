# Compiled-program visualizer

See what a demonstration compiled **into**. A compiled bundle is not a video —
it is a governed program: an ordered set of steps, each carrying how its target
is re-resolved, whether an identity gate protects the click, what real
system-of-record effect must hold, what the screen must look like afterward, its
risk class, and where the run will **halt** rather than guess. The visualizer
renders that structure.

![Program graph of the OpenEMR showcase bundle](showcase-openemr/program-graph.png)

## One spec, three surfaces

The engine is the single source of truth. `openadapt_flow.visualize`
**emits a serializable _program-graph spec_** from a compiled bundle; every
surface renders that spec and none of them re-parse the bundle IR:

- **CLI** (`openadapt-flow visualize`) — self-contained HTML / Mermaid / JSON.
- **Cloud** (`app.openadapt.ai`) — an interactive React view over the same spec.
- **Desktop** (Tauri app) — a view that vendors the same renderer.

The spec is versioned and has a committed JSON Schema
(`schemas/program-graph-v1.json`) so non-Python surfaces validate the same
shape. A sample emitted spec lives at
[`docs/showcase-openemr/program-graph.json`](showcase-openemr/program-graph.json).

### Spec shape (v1)

```
ProgramGraphSpec
├─ spec_version
├─ bundle: BundleMeta      # name, schema version, PHI/encryption flags,
│                          # provenance/certification, params, rollup counts
├─ nodes: [GraphNode]      # one per compiled step / program state
│   ├─ kind                # action | branch | business_decision | loop |
│   │                      # subflow_call | terminal
│   ├─ title, action, risk # intent, click/type/…, reversible|irreversible
│   ├─ resolution          # the target-resolution LADDER + which rung is top
│   ├─ identity            # armed? phi-free? unarmed-reason?
│   ├─ effects             # system-of-record checks
│   ├─ postconditions      # vision verification kinds
│   ├─ guard / wait_until  # control-flow preconditions
│   ├─ halts               # fail-safe HALT points this node introduces
│   └─ badges
└─ edges: [GraphEdge]      # sequence | branch | exception | loop_body
```

Nodes = steps. Edges = sequence, with typed room for branches / loops /
exception paths. Annotations = verification points and halt points. A linear
bundle (today's common case) projects to a straight chain of `action` nodes
ending in a `success` terminal; a Phase-2 program graph projects its full state
machine 1:1, so richer compiled structure renders without a spec break.

## CLI

```bash
# self-contained HTML (opens offline; no network, CSP-safe)
openadapt-flow visualize path/to/bundle -o program.html

# Mermaid flowchart source for Markdown / docs / a PR description
openadapt-flow visualize path/to/bundle --format mermaid

# the shared JSON graph spec (what the cloud + desktop surfaces render)
openadapt-flow visualize path/to/bundle --format json -o program-graph.json
```

## Rendering choice & tradeoffs

- **Engine emits the spec; surfaces render it** — rather than each surface
  re-parsing the bundle IR. This keeps one projection of the compiled semantics
  and a single wire contract, and lets the cloud/desktop surfaces render without
  a Python engine on hand.
- **Custom lightweight layout, not a graph library.** The compiled program is a
  vertical sequence with room for branches, and the value is in the **per-node
  annotations** (resolution ladder, identity gate, effect check, halt points) —
  far clearer as node _cards_ than as edges-and-boxes. A full graph lib
  (d3/cytoscape/reactflow) is heavy overkill and would break the
  self-contained/CSP-safe requirement. Mermaid is offered as a portable
  secondary format; JSON for tooling.
- **Self-contained HTML.** The CLI inlines the shared CSS + a dependency-free
  vanilla-JS renderer (`openadapt_flow/visualize/static/program_graph.{css,js}`)
  and embeds the spec as JSON, so the page opens offline and renders under a
  strict CSP. The desktop (Tauri, CSP `'self'`) vendors those same two files.

## What `visualize` shows: the bundled MockMed sample

This is the actual Mermaid that `visualize` emits for the bundled MockMed
triage sample, produced by
`openadapt-flow visualize docs/showcase/bundle --format mermaid` (nothing
below is hand-drawn):

```mermaid
flowchart TD
  n0("click recorded visual target<br/><small>visual template + 2 OCR landmarks</small>")
  n1("type 'nurse.demo'")
  n2("click recorded visual target<br/><small>visual template + 2 OCR landmarks</small>")
  n3("type 'mockmed-demo-pass'")
  n4("click 'Sign In'<br/><small>visual template + 2 OCR landmarks</small>")
  n5("click 'Open'<br/><small>visual template + 2 OCR landmarks</small>")
  n6("click 'New Encounter'<br/><small>visual template + 2 OCR landmarks</small>")
  n7("click 'Triage'<br/><small>visual template + 2 OCR landmarks</small>")
  n8("click recorded visual target<br/><small>visual template + 2 OCR landmarks</small>")
  n9("type <note>")
  n10("click 'Save Encounter'<br/><small>visual template + 2 OCR landmarks</small>")
  n11{{"Success"}}
  n0 --> n1
  n1 --> n2
  n2 --> n3
  n3 --> n4
  n4 --> n5
  n5 --> n6
  n6 --> n7
  n7 --> n8
  n8 --> n9
  n9 --> n10
  n10 --> n11
  classDef irreversible stroke:#b4530a,stroke-width:2px;
  classDef halt stroke:#b21f2d,stroke-width:2px;
```

How to read the target labels:

- **`recorded visual target` is not coordinate replay.** It means the control
  had no readable label, so the bundle retained its visual crop and nearby text
  instead. The demonstration's point is only the relative offset inside the
  target after that evidence re-finds it.
- **`visual template + 2 OCR landmarks` names the retained evidence.** Replay
  resolves it on a fresh frame; global movement is accepted only when the
  landmarks do not contradict it, and ambiguous OCR refuses instead of picking
  a match.
- **DOM/accessibility is stronger when available.** Browser and native bundles
  show that structural rung instead; RDP and Citrix intentionally use the
  visual floor.
- **The HTML view carries the full contract.** `--format html` expands every
  resolution rung, identity gate, effect check, postcondition, and halt point.

*Text summary (for renderers without Mermaid): the compiled MockMed triage
bundle signs in, opens the patient, starts an encounter, enters the `<note>`
parameter, and saves it. Each click is re-found from retained evidence rather
than replayed at a literal screen coordinate.*
