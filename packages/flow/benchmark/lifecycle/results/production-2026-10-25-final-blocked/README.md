# Final run on main + #1516: blocked at admission (2026-10-25)

This was meant to be the third 55-item run of the same stream as
[`production-2026-10-25-rerun`](../production-2026-10-25-rerun/README.md),
on origin/main 6dc0be40e plus #1516 (4b635f2d6) plus the harness. Branch
`measure/production-lifecycle-final` at **9eb8912a0**. Host load was 20.9
at the start, below the 40 limit.

No item ran. The program could not be admitted, so there are no
throughput numbers and no Phase 0 verdict from this run.

## What failed

Admission qualification, representative case, 2 of 2 attempts:

> Step 'step_015' (scroll by (0, 300)): closed-loop scroll exhausted its
> budget (600px of 750px allowed, 2.5x the recorded distance) without the
> anchor of step 'step_016' (click recorded visual target) resolving

The same program admitted and ran at 38fc21afb.

## Cause

9042f1603 (#1504, "halt before a write when the rows cannot be counted")
added a candidate census to every verified identity check. Logging the
census during the representative case:

| Step | Mode | Ladder verdict | After census |
| --- | --- | --- | --- |
| step_000 to step_012 | structured | verified | verified (`not_a_row`) |
| step_016 | pixel | verified | **abstain, `identity_uncounted`** |

step_016 is a click on a form field, resolved by its pixel template. The
OCR census can't count rows there (`cannot_count`), and a save follows, so
the census turns the verified match into a halt. The closed-loop scroll
reads that as "anchor not in view" and keeps scrolling until its budget
runs out.

The commit message says a target that teach time recorded as a form field
or control is `not_a_row`, and its verdict stands. The code applies that
exemption only when `check.mode == "structured"`
(`Replayer._apply_candidate_census`). So a pixel-resolved form field is
treated as an uncountable row.

With the census turned off for the probe (no other change), the
representative case verified (`probe-census-off-QUALIFICATION.md`; the
other five cases weren't run in the probe, so that file reads FAIL). That
is a diagnosis only. It isn't a fix and wasn't used for any measurement.

## Files

- `run.log`: the console output, with the commit and the traceback.
- `probe-census-off-QUALIFICATION.md`: the representative case with the
  census disabled.
