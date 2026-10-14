# openadapt-flow 1.27.1 -> 1.28.0, release over release

Both measurements ran the same two runners on the same host against the same
bundled MockMed application. This is the first release-over-release comparison
in this directory in which the fixture *and* both harness files were
byte-identical, so nothing below can be an artefact of measuring differently.

## What was held constant, exactly

| | 1.27.1 (2026-08-02) | 1.28.0 (2026-08-02) | Held? |
|---|---|---|---|
| Comparison runner SHA-256 | `68f9e5a2…8a126` | `68f9e5a2…8a126` | **yes, byte-identical** |
| Probe runner SHA-256 | `097cc900…40ce8` | `097cc900…40ce8` | **yes, byte-identical** |
| MockMed fixture tree | `f0736c7a82ca2aba2beb334feaf461c5f06532a5` | `f0736c7a82ca2aba2beb334feaf461c5f06532a5` | **yes, byte-identical** |
| Host / Python / Playwright | macOS 15.7.3 arm64, 3.12.13, 1.61.0 | macOS 15.7.3 arm64, 3.12.13, 1.61.0 | yes |
| Trials, arms, conditions | 3 x 3 x 3, no retries | 3 x 3 x 3, no retries | yes |
| Evals commit | `8132e607` | `71edc889` | no — docs only, no runner touched |

The evals commit moved because the 1.27.1 evidence itself landed in between.
That commit changed `README.md` and files under `docs/eval_results/`; it touched
no runner and no script. The two harness digests above are the load-bearing
check, and both are unchanged.

## What is in 1.28.0

Three commits, plus the release commit:

- `c9618cc` `fix: deliver consequential remote clicks through the frame lease`
- `e054caa` `feat: carry managed Execute authority through BYOC`
- `ccdd155` `fix: simplify the RDP buyer presentation`

Only `c9618cc` changes replay behaviour, and only on the *remote* actuation
path. The comparison and the transaction probe both drive a local browser
backend, so neither exercises it. It is measured directly instead, in
`remote_lease_safety/`.

## Delta 1: no outcome classification moved. None.

Every counted cell reports the same classification as on 1.27.1.

| Condition | Arm | 1.27.1 primary / replication | 1.28.0 primary / replication |
|---|---|---|---|
| `clean` | compiled replay | 3/3, 0 over-halt / 3/3, 0 over-halt | 3/3, 0 over-halt / 3/3, 0 over-halt |
| `clean` | DOM positional | 3/3 / 3/3 | 3/3 / 3/3 |
| `clean` | DOM name-scoped | 3/3 / 3/3 | 3/3 / 3/3 |
| `theme` | compiled replay | 3/3, 0 over-halt / 3/3, 0 over-halt | 3/3, 0 over-halt / 3/3, 0 over-halt |
| `theme` | DOM positional | 3/3 / 3/3 | 3/3 / 3/3 |
| `theme` | DOM name-scoped | 3/3 / 3/3 | 3/3 / 3/3 |
| `rename` | compiled replay | 3/3 / 3/3 | 3/3 / 3/3 |
| `rename` | DOM positional | 0/3 / 0/3 | 0/3 / 0/3 |
| `rename` | DOM name-scoped | 0/3 / 0/3 | 0/3 / 0/3 |

Denominators: 3 trials per arm per condition, which is the minimum this
repository's evaluation standard admits for a comparative claim; the runner
fixes it at 3 and the independent replication doubles every cell to 6. One
measurement run is 27 counted trials (3 conditions x 3 arms x 3 trials), so the
primary plus the replication is 54, of which 18 are compiled replay.

Across those 18 compiled trials: 18/18 task success, 0 over-halts, 0 silent
incorrect successes, 0 wrong-target writes, 0 model calls, $0.00. Identity
coverage is unchanged at 5 of 8 applicable clicks armed. The
`clean_postcondition_over_halt.json` and `theme_postcondition_over_halt.json`
artifacts record 0 observations in the primary run and 0 in the replication, as
they did on 1.27.1.

## Delta 2: the transaction probe is unchanged, cell for cell

All 10 cells (5 fault modes x 2 verification configurations) report exactly the
same transaction outcome and the same ground-truth business effect as on
1.27.1, over 3 trials per cell = 30 counted runs.

| Invariant | Applicable runs | Violations | 1.27.1 | 1.28.0 |
|---|---:|---:|---|---|
| `COMPLETED_UNVERIFIED` is never a production success and never billable | 12 | 0 | holds | holds |
| Only `VERIFIED` may be billable | 30 | 0 | holds | holds |
| An outcome asserting no business effect must not be reported when the record shows the write landed | 0 | 0 | **vacuous** | **vacuous** |
| A consequential step that reached actuation but was never verified must be `RECONCILIATION_REQUIRED` | 21 | 0 | holds | holds |
| A single run must never write the intended record more than once | 24 | 0 | holds | holds |
| `VERIFIED` must never be reported when nothing landed | 0 | 0 | **vacuous** | **vacuous** |
| A healthy compiled run makes no model calls and costs $0 | 30 | 0 | holds | holds |

Zero violations across 30 counted runs, unchanged. Two of the seven invariants
are `vacuous`: no counted run claimed an absence and no counted run reported
`VERIFIED`, so no run could exercise them. A vacuous invariant proves nothing
and is not a pass. It is reported that way deliberately, in both releases.

The `timeout` fault mode — commit the row, then hang past the client timeout —
reports `RECONCILIATION_REQUIRED` in all 6 runs, exactly as on 1.27.1. This is
where the 1.24.0 evidence recorded `HALTED_BEFORE_EFFECT` in 6 of 6 runs, and
where the 1.24.0 probe failed `no_false_absence_claim` in 7 of 16 applicable
runs and `unverified_delivered_write_needs_reconciliation` in 14 of 23. Those
were fixed in 1.25.0 and they stay fixed here.

## Delta 3: one number moved, and it moved the wrong way

Compiled replay is slower on 1.28.0 in every condition, in both the primary run
and the replication.

| Condition | 1.27.1 steady median | 1.28.0 steady median | Delta |
|---|---:|---:|---:|
| `clean` primary | 6.828s | 7.033s | +0.205s (+3.0%) |
| `clean` replication | 6.860s | 7.030s | +0.171s (+2.5%) |
| `theme` primary | 7.003s | 7.255s | +0.252s (+3.6%) |
| `theme` replication | 7.025s | 7.190s | +0.165s (+2.3%) |
| `rename` primary | 6.837s | 7.031s | +0.194s (+2.8%) |
| `rename` replication | 6.934s | 7.033s | +0.099s (+1.4%) |

Six of six cells moved the same way. In the `clean` condition the two samples do
not overlap at all: the six 1.27.1 trials span 6.801-6.947s and the six 1.28.0
trials span 6.966-7.055s. That is a small, consistent slowdown and it is
reported rather than rounded away.

**What this design can conclude, and what it cannot.** The harness and the
fixture were byte-identical, so a measurement artefact is excluded. It cannot
separate an engine effect from host drift: the two evidence sets were measured
in separate sessions a few hours apart on one machine, not interleaved, so a
change in machine state between the sessions produces exactly this signature.
Record and compile timings, measured in the same sessions, did not move
(compile 8.151 / 6.407 / 6.374s here against 8.141 / 6.413 / 6.464s on 1.27.1),
which argues against a broad host slowdown but does not settle it. Nothing in
`c9618cc` runs on this path.

Establishing the cause needs an interleaved A/B of the two wheels in one
session. That is not run here, so the delta is recorded as unattributed rather
than assigned to the engine. It changes no outcome classification.

## Delta 4: the remote frame-lease change, measured directly

`c9618cc` lets a consequential remote click be delivered through the backend's
one-shot actuation lease when no typed delivery receipt exists, so that an
opaque pixel-only surface stops over-halting on its write step. Relaxing a
refusal is the kind of change that can buy a green demonstration with a silent
wrong write, so the release note's two safety properties were measured, not
assumed. `remote_lease_safety/` holds the artifacts; 3 cells x 3 trials = 9
counted runs, each classified under all 3 execution profiles = 27 classifications.

| Cell | Runs | Input edges delivered | Typed receipts | `transaction_outcome` under demo / standard / regulated |
|---|---:|---:|---:|---|
| `ungoverned_lease` | 3 | 3 | 0 | `COMPLETED_UNVERIFIED` in all 3 profiles |
| `governed_lease` | 3 | **0** | 0 | `HALTED_BEFORE_EFFECT` in all 3 profiles |
| `lease_frame_changed` | 3 | **0** | 0 | `RECONCILIATION_REQUIRED` in all 3 profiles |

- **A governed run still refuses.** 3 of 3 governed runs stopped before the
  first input edge with `safety_halt` set and the error `Step 's1' (click
  'Save') is a consequential remote click, but this backend cannot bind its
  exact fresh frame and target to delivery; run aborted`. The backend received
  zero input edges. Only the presence of a `GovernedRunAuthorization`
  distinguishes this cell from `ungoverned_lease`, which delivered 3 of 3.
- **The lease delivery is never `VERIFIED`.** Over 9 counted runs x 3 profiles
  = 27 classifications, `VERIFIED` was returned 0 times, `production_eligible`
  was true 0 times, and `transaction_billable` was true 0 times. Every
  lease-delivered run carried no delivery receipt and no actuation tier, which
  is what makes `COMPLETED_UNVERIFIED` the classifier's only available answer.
- The lease is a real safety property, not a formality: when the remote frame
  changed between `acquire_actuation_frame` and the input edge, the backend
  received zero input edges in 3 of 3 runs, and the run settled at
  `RECONCILIATION_REQUIRED` rather than claiming a proven absence — the runtime
  could not prove the click had not landed, so it did not say so.

Scope: the backend in that probe is a fake exposing only the two-phase lease.
It measures a runtime contract. It is not a real Citrix or RDP session and no
wrong-target-immunity claim on a real remote surface follows from it.

## What did not change

- The DOM selector controls still fail 0/3 under `rename` in both releases, and
  still fail loudly at the first renamed locator before any mutation. They are
  halts, not silent wrong actions.
- Identity coverage remains 5 of 8 applicable clicks armed. The three unarmed
  steps carry the same recorded reasons.
- Zero silent incorrect successes, zero wrong actions, zero model calls, $0.00,
  in every counted trial of both releases.
- No zero-shot comparison is claimed in either release. It remains not run: it
  needs a paid model and a paid WAA environment, and the live replay path still
  lacks a wired WAA evaluator.

## Scope

One synthetic workflow, one macOS host, headless Chromium, three trials per
cell, doubled by one independent replication of the comparison. No hosted
lifecycle, Windows UIA, RDP, Citrix, or real customer application is
represented. This release-over-release comparison establishes that 1.28.0
changed no measured outcome on this workflow; it is not a general reliability
claim.
