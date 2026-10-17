# Compact data-driven LOOP showcase (`program:true`)

A small, legible companion to [`../showcase-loop`](../showcase-loop) (which
wraps the 18-step OpenEMR recording). This one is deliberately tiny so its
program graph reads at a glance and fits in the launcher README, while still
showing the load-bearing structure: the `LOOP`, the per-record body, the effect
check, the irreversible-write halt point, and the loop-back edge.

Two bundles are emitted from one authored `save-encounter` body:

- `body/` — the single demonstration, a linear `program:false` bundle (the
  straight-line case `visualize` renders as a chain);
- `bundle/` — that same body wrapped in a **`LOOP`** over
  [`worklist.csv`](worklist.csv) (`program:true`), which the Phase-2
  interpreter runs **once per record**, binding each record's `patient_id` and
  `note` columns to the workflow's parameters.

The `save` step is a consequential, irreversible write carrying **param-bound**
system-of-record effects (`record_written` + `field_equals`), so each iteration
is verified against the value it actually wrote — iteration *N* confirms the
right record or HALTs.

The runtime executes the loop:

- **bounded** — a worklist longer than the loop's `max_iterations` HALTs
  (fail-safe), never runs unbounded;
- **`$0`, zero-model** — loop iteration, per-row binding, and worklist
  resolution are all deterministic;
- **identity-gated & effect-verified per record** — every iteration re-runs the
  same hardened per-action pipeline the linear replayer uses;
- **halt-on-ambiguity** — a poisoned/ambiguous record triggers a safe HALT (no
  wrong write, no silent skip).

## Honesty note

The body is a **MockMed-shaped, hand-authored fixture** — no PHI, no recorded
pixels. It is a real, loadable bundle whose loop actually **interprets** once
per record: `tests/test_loop_authoring.py::`
`test_committed_encounter_showcase_loop_replays_through_the_interpreter` replays
this exact committed bundle through the real `Replayer._interpret_program`
against the in-process MockMed system of record, verifying every record's write
and asserting zero model calls. The graph in the README is that interpreted
program, not a drawing. For a loop authored over a **real recording**, see
[`../showcase-loop`](../showcase-loop).

## Visualize

```bash
openadapt flow visualize docs/showcase-encounter-loop/body   --format mermaid   # linear
openadapt flow visualize docs/showcase-encounter-loop/bundle --format mermaid   # loop
```

## Regenerate

```bash
python scripts/build_showcase_encounter_loop_bundle.py
```

Deterministic: the emitted bundles are a pure function of the body defined in
the script and `worklist.csv`, so re-running reproduces the committed artifacts
byte-for-byte.

## Remaining gap

The worklist here is a **provided dataset**. Reading the records **off the
screen** mid-run (e.g. "process every row currently in this on-screen queue")
needs a screen→value extraction primitive that does not exist yet. This
showcase covers the provided-worklist case; the on-screen-worklist case is
future work.
