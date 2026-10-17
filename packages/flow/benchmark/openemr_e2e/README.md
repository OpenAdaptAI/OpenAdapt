# OpenEMR end-to-end proof harness (compiled arm)

An **integrated** end-to-end harness that ties the whole compiled runtime
pipeline together against the OpenEMR **add-patient-note** flagship task, in one
reproducible, model-free ($0) run:

```
compile → replay → effect-verify (system of record) → catch a silent wrong write
       → inject drift → HALT → teach the fix (governed) → re-run clean
```

It does **not** duplicate the existing OpenEMR *benchmark*
(`openadapt_flow.benchmark.openemr_benchmark`, which measures the compiled arm
vs. a paid agent arm on the **live** public demo and is explicitly *not*
CI-reproducible). This harness *orchestrates* the runtime components that
benchmark assumes work — the compiler, the `Replayer`, the `EffectVerifier`, the
halt path, and the governed teach/learn loop — and asserts they **compose** end
to end, on every push.

## Run it

```bash
python -m benchmark.openemr_e2e --out /tmp/openemr-e2e
```

Writes `result.json` (structured, per-phase) and `SUMMARY.md` (a short markdown
table) into `--out`, plus the compiled `bundle/`, per-phase `runs/`, and the
promoted `skills/` library.

## What it proves (fixture path, CI-reproducible)

Six phases, each a **real** runtime call, all deterministic and offline (the
in-process MockMed `fault_server` is the system of record):

1. **compile** — the add-note demonstration is materialized as a compiled
   bundle (`workflow.json` + templates) whose Save step carries two
   system-of-record `Effect` contracts (`record_written`, `note` `field_equals`).
2. **clean replay + effect-verify** — a real `Replayer.run` completes and the
   note write is **CONFIRMED** against the record (not the screen).
3. **silent-wrong-write catch** — under a `partial` fault the screen still
   paints “Saved”, but the record drops the note; effect verification
   **REFUTES** and the run **HALTS**. Screen-only checking would have passed —
   this is the flagship “silent wrong action” catch.
4. **inject drift → HALT** — an unexpected consent modal blocks the confirm
   step; the never-demonstrated workflow **HALTS** and emits a learnable
   `HaltObservation` (it refuses to guess).
5. **teach** — the operator’s dismiss-then-confirm correction is fed to the
   **governed** learn/promote loop, which induces a guarded branch, gates it
   (identity/effect/risk may not regress), and promotes only on improved
   held-out coverage.
6. **re-run clean** — the taught program re-runs the **same** drift to success
   (dismisses the modal, confirms), while a clean run **skips** the new branch —
   proving no regression.

Each run uses a **distinct** note value, so a pass proves parameter substitution
against real state, not replay of a baked-in literal.

## Live vs. fixture (never a silent skip)

The end-to-end loop **always** runs on the fixture substrate — that is the
CI-reproducible proof, and every result is labelled `substrate="fixture"`.

When `OPENEMR_FHIR_BASE_URL` (and optionally `OPENEMR_FHIR_TOKEN`) is set, the
harness **additionally** probes the **real** OpenEMR FHIR system of record for
reachability (a genuine `FhirEffectVerifier.capture_pre_state` against live
state) and records the outcome honestly under `live_probe`. Pass
`--require-live` to make an unreachable live SoR a hard error rather than a quiet
pass. A skipped live probe is always disclosed in `result.json` / `SUMMARY.md`;
the harness never reports a live result it did not obtain.

## Cost guardrail (do not violate)

- The **compiled arm is model-free by construction**: no API client is ever
  constructed, so `cost_usd == 0.0` and `model_calls == 0`. Enforced, not
  promised.
- The **paid computer-use agent arm** — the money-spending comparison — is wired
  **only as a gate**. `--agent-arm` requires a hard `--max-cost-usd` cap, and
  even with both supplied this harness **refuses to invoke it** (raises
  `AgentArmRefused`; exit code 2). Nothing here ever calls a paid API.
- The head-to-head **ratio** (compiled vs. agent) is reported from
  **previously-recorded** numbers in `benchmark/openemr/results.json` — never by
  spending money now.
- To actually run the paid arm (with the full audited guardrails: per-run and
  total cost caps, API preflight, billing-error abort), use the separate,
  purpose-built path:

  ```bash
  python scripts/openemr_demo.py benchmark
  ```

## Files

- `harness.py` — `run_openemr_e2e(...)`, the six phases, the agent-arm gate, the
  live probe, and the `result.json` / `SUMMARY.md` writers.
- `simulation.py` — the deterministic fixture substrate: `SimBackend` (Save
  writes to the real system of record), `AddNoteVision` (drift-aware scripted
  vision), and `build_add_note_program()` (the compiled add-note workflow with
  effects).
- `__main__.py` — the CLI.
