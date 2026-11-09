# Data-driven LOOP showcase (`program:true`)

This bundle is the proof that the **demonstration → governed data-driven loop**
authoring wire is reachable end to end on a real bundle.

`bundle/` is produced by wrapping the existing single-demonstration OpenEMR
showcase bundle (`../showcase-openemr/bundle`, which is `program:false` — a
linear list of steps) in a **`LOOP`** that runs the demonstrated body **once per
record** of `worklist.csv`, binding each record's `note` column to the
workflow's `note` parameter.

The result is a `program:true` bundle whose Phase-2 `ProgramGraph` the runtime
executes:

- **bounded** — a worklist longer than the loop's `max_iterations` HALTs
  (fail-safe), never runs unbounded;
- **`$0`, zero-model** — loop iteration, per-row binding, and worklist
  resolution are all deterministic;
- **identity-gated & effect-verified per record** — every iteration re-runs the
  same hardened per-action pipeline (identity gate, effect verifier) the linear
  replayer uses, so iteration *N* acts on the right record or HALTs;
- **halt-on-ambiguity** — a poisoned/ambiguous record triggers a safe HALT (no
  wrong write, no silent skip).

## Regenerate

```bash
python scripts/build_showcase_loop_bundle.py
```

Deterministic: the emitted bundle is a pure function of the source bundle and
`worklist.csv`.

## Author your own

```bash
openadapt-flow for-each <single-demo-bundle> \
    --records worklist.csv \
    --out my-loop-bundle \
    [--map <column>=<param> ...] \
    [--max-iterations N]
```

Every worklist column must map to a known, non-secret workflow parameter, and
every parameter the body binds must be supplied by the worklist or carry a demo
default — otherwise authoring **fails loudly** rather than emitting a
silently under-bound bundle.

## Honest remaining gap

The worklist here is a **provided dataset**. When the records must instead be
**read off the screen** mid-run (e.g. "process every row currently in this
on-screen queue"), that needs a screen→value extraction primitive that does not
exist yet (`ActionKind` has no `read`/`extract`, and `entity_ref` run-time
re-resolution is a slot, not a behavior). This showcase covers the
provided-worklist case; the on-screen-worklist case is future work.
