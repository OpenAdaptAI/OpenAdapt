# openadapt-flow 1.24.0 -> 1.27.1, release over release

Both measurements ran the same two runners on the same host against the same
bundled MockMed application. This is the first release-over-release comparison
in this directory where the target application really was held constant.

## What was held constant, exactly

| | 1.24.0 (2026-07-27) | 1.27.1 (2026-08-02) | Held? |
|---|---|---|---|
| Comparison runner SHA-256 | `ac58c0b9…5970f` | `68f9e5a2…8a126` | functionally, yes — see below |
| Probe runner SHA-256 | `097cc900…40ce8` | `097cc900…40ce8` | yes, byte-identical |
| MockMed fixture tree | `f0736c7a82ca2aba2beb334feaf461c5f06532a5` | `f0736c7a82ca2aba2beb334feaf461c5f06532a5` | **yes, byte-identical** |
| Host / Python / Playwright | macOS 15.7.3 arm64, 3.12, 1.61.0 | macOS 15.7.3 arm64, 3.12, 1.61.0 | yes |
| Trials, arms, conditions | 3 x 3 x 3, no retries | 3 x 3 x 3, no retries | yes |

The comparison runner digest moved because of two commits, `1801027` (replace a
hard-coded 1.16.1 reproduce string with a formatted one; metadata in an emitted
JSON file) and `c4b7e9b` (delete one blank line during a ruff cleanup). Neither
touches an arm, the oracle, the classification rules, the trial count, or the
retry policy.

The fixture caveat that governed the 1.16.1 -> 1.24.0 comparison therefore does
**not** apply here. MockMed ships inside the Flow wheel, so repinning the wheel
can repin the application; between these two releases it did not. Every delta
below is attributable to the engine.

## Delta 1: the `clean` over-halt is gone, and a named engine fix explains it

| Condition | Arm | 1.24.0 | 1.27.1 (primary) | 1.27.1 (replication) |
|---|---|---|---|---|
| `clean` | compiled replay | 3/3 succeeded, **2/3 over-halt** | 3/3 succeeded, 0/3 over-halt | 3/3 succeeded, 0/3 over-halt |
| `theme` | compiled replay | 3/3 succeeded, 0/3 over-halt | 3/3 succeeded, 0/3 over-halt | 3/3 succeeded, 0/3 over-halt |
| `rename` | compiled replay | 3/3 succeeded, 0/3 over-halt | 3/3 succeeded, 0/3 over-halt | 3/3 succeeded, 0/3 over-halt |

`clean_postcondition_over_halt.json` records 0 observations in both the primary
run and the replication, against 2 in each of the 1.24.0 run and its
replication.

The cause recorded for 1.24.0 was that the compiler's largest-changed-region
search had settled the `step_010` `region_stable` postcondition onto the
saved-encounter row, which renders the trial-unique `note` **parameter**. Each
trial's verdict then turned on the exact glyphs of that trial's note, so a
correctly saved encounter was reported as a halt.

openadapt-flow `c068554`, released in 1.25.0, is
`fix(compiler): stop a parameter's demonstrated value becoming a pixel
invariant (#285)` — precisely that defect. With the fixture byte-identical
across the two releases, that fix is the explanation, and the over-halt does not
reappear in 18 counted compiled trials across two independent runs.

Compiled `clean` end-to-end median also drops from 12.888s to 7.408s, because
the over-halting trials no longer spend their postcondition-retry budget before
giving up.

## Delta 2: the false-absence defect is fixed

The 1.24.0 transaction probe was the "Unsound" finding: uncertain delivery did
not land in `RECONCILIATION_REQUIRED`. Two invariants failed.

| Invariant | 1.24.0 | 1.27.1 |
|---|---|---|
| An outcome asserting no business effect must not be reported when the system of record shows the write landed | **FAILED**, 7 of 16 applicable runs | holds, 0 violations (vacuous: 0 applicable runs) |
| A consequential step that reached actuation but was never verified must be `RECONCILIATION_REQUIRED`, not a proven absence | **FAILED**, 14 of 23 applicable runs | holds, 0 of 21 applicable runs |
| `COMPLETED_UNVERIFIED` never a production success, never billable | holds, 11 runs | holds, 12 runs |
| Only `VERIFIED` may be billable | holds, 30 runs | holds, 30 runs |
| A single run must never write the intended record more than once | holds, 24 runs | holds, 24 runs |
| A healthy compiled run makes no model calls and costs $0 | holds, 30 runs | holds, 30 runs |

The behaviour that changed is exactly the one that was wrong: `timeout` — the
fault mode that commits the row and then hangs past the client timeout — was
reported as `HALTED_BEFORE_EFFECT` in 1.24.0 and is reported as
`RECONCILIATION_REQUIRED` in 1.27.1, in all 6 runs across both verification
configurations. `session`, which persists nothing, also moved to
`RECONCILIATION_REQUIRED` rather than claiming a proven absence it had not
verified.

openadapt-flow `11c115c`, released in 1.25.0, is
`fix(transaction): require positive evidence of absence for HALTED_BEFORE_EFFECT
(#280)`.

The first invariant is now marked `vacuous` rather than `yes`: with no run
claiming an absence, no counted run can exercise it. A vacuous invariant proves
nothing and is not a pass. It is reported that way deliberately.

## What did not change

- The DOM selector controls still fail 0/3 under `rename` in both releases, and
  still fail loudly at the first renamed locator before any mutation. They are
  halts, not silent wrong actions.
- Identity coverage remains 5 of 8 applicable clicks armed. The three unarmed
  steps carry the same recorded reasons.
- Zero silent incorrect successes, zero wrong actions, zero model calls, $0.00,
  in every counted trial of both releases.
- No zero-shot comparison is claimed in either release. It remains not run.

## Scope

One synthetic workflow, one macOS host, headless Chromium, three trials per
cell. No hosted lifecycle, Windows UIA, RDP, Citrix, or real customer
application is represented. Two engine fixes are attributed by name here because
the fixture and the harness were held constant; that is attribution from a
controlled comparison, not a general reliability claim.
