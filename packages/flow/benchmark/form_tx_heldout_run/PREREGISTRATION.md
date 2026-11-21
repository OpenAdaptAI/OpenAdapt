# Pre-registration: form layer on held-out synthetic quote portals

Written 2026-11-21, before any form-layer run on these portals and before
the author of the form layer opened the portal code, cases or answer key.
Changes after the first scored run go under "Deviations" with a date and a
note on whether scored data existed at the time.

## Question

On carrier-style quote portals the layer was not built against, how often
does `openadapt_flow.form_tx` submit a correct quote, stop a wrong value
before submit, stop when nothing was wrong, leave a field unchecked, and hand
work back to the operator? Does rule learning (`form_tx/learning.py`) reduce
hand-backs without letting a wrong value through?

## What is frozen

- **Engine:** OpenAdapt branch `claude/form-tx-weekend` at the commit named
  in `ENGINE_SHA` next to this file. It includes the complete, fail-closed
  read-back: every typed, selected or kept field gets a `passed`, `failed`
  or `not_checked` verdict, and any `not_checked` field makes the outcome
  `readback_incomplete`, never `verified_done`.
- **Portals and answer key:** branch `claude/form-tx-heldout-portals` at
  `36be56dcd6fdc9adc97567bce272ab81d2a528ff`, written by a separate agent
  that was told not to read the form layer. `answer_key.json` sha256:
  `948f695aaddcb1d30db0316d1da6af73fce7adfa6b60dc5c5ef14edc8ec27227`.
  Four portals (`harborline`, `cobalt`, `pinecrest`, `tidewater`), one dev
  case each and 32 scored cases (9 + 8 + 8 + 7), 12 of them clean.
- **Oracle:** the portal's own stored record (`GET /__oracle/submissions`),
  scored by the portal author's `score_case`, which compares every stored
  field with the answer key. The layer never sees the oracle endpoint.

## Procedure

1. After the engine SHA is pushed and this file is pushed, the form-layer
   author opens the portals and **only the dev case** of each portal, and
   writes one form pack (data) per portal from it. Where
   `form_tx.pack_compiler.induce_form_pack` can produce a binding, it is
   used; the rest is written by hand. The results say which parts were
   hand-written. Packs are committed before the first scored run.
2. Smoke: each pack is run on its dev case only, until the dev case passes or
   the pack is declared unworkable. Engine code is not changed in this step.
3. Scored run: every scored case runs **once**, per arm, in the order given
   by `spec.cases`, headless Chromium on the zenbook lab, a fresh
   `load_case` before each case, 180 s wall limit per case.
4. Any engine or pack change after the first scored case is a labelled
   post-hoc row, in its own PR, and never replaces the frozen row.

## Arms

- **A, unguarded:** the portal author's `naive_filler.py` (fills by label,
  first type-ahead suggestion, accepts the estimator, submits without
  checking). It shows how hard the traps are. Its numbers are already in
  `REFERENCE_RUNS.json` and are re-run once here unchanged.
- **B, form layer, learning off.**
- **C, form layer, learning on:** one `RuleAccumulator` across the portal's
  scored cases in run order. The scripted operator sets
  `remember_as_candidate_rule=True` on every answer. After each case,
  candidates with support of at least 2 runs are admitted with a fixed
  approval id and a qualification check that replays the dev case on the
  new pack and requires `verified_done` with a correct oracle record. The
  anchor for an admitted binding is the halted control's visible label.

## Scripted operator (B and C)

On each halt, the operator looks up `operator_script` in the answer key, by
the halt's field label first, then its dossier key, and takes that action
(`provide_value` with the scripted value, `accept_portal_prefill`, or
`fixed_on_screen` after a sign-in with the case's `session_login`). With no
script entry, the operator escalates to the owner, which ends the case
unsubmitted. Every halt shown to the operator counts as one hand-back.
More than 12 halts on one case aborts it.

## Metrics (per arm; denominators are the 32 scored cases unless stated)

- **Correct submissions:** `score_case(...).correct` (every stored field
  right and exactly one submission). Also reported: correct and
  `verified_done`.
- **Wrong submitted:** submitted with `record_correct` false. Split by the
  layer's final outcome: `verified_done` (a false proof), `readback_refuted`
  or `readback_incomplete` (flagged after submit), anything else (silent).
- **Wrong values stopped before submit:** over the 20 trap cases (expected
  behavior `must_stop_before_submit` or `needs_operator_answer`), the cases
  where the layer halted at least once before the first submission. Also
  reported: of those, how many then ended with a correct record.
- **False stops:** over the 12 clean cases (`submit_clean`), the cases with at
  least one halt before submission, and the number of such halts. A stop on
  a trap case whose `also_acceptable` includes `submit_clean` is not a false
  stop.
- **Fields not checked:** total `not_checked` field verdicts, and the number
  of cases that ended `readback_incomplete`.
- **Operator hand-backs:** total halts shown to the operator, plus post-submit
  flags (`readback_refuted` / `readback_incomplete`), and per-case mean.
- **Unsubmitted:** cases with no stored record (escalated, aborted, timeout).

Every number is reported, per portal and pooled, including failures and
cases the layer could not run. No case is dropped.

## Reject criteria (fixed now)

The proposal may only say a claim the data supports. Each line kills the
matching claim:

- **R1, "a receipt means the quote is right":** killed by any wrong submitted
  with `verified_done`.
- **R2, "it never lets a wrong value through silently":** killed by any wrong
  submitted whose outcome is not a halt, `readback_refuted` or
  `readback_incomplete`.
- **R3, "it stops wrong values before submit":** stated only as the measured
  fraction; called "most" only if at least 75% of trap cases stopped before
  submit.
- **R4, "it does not stop clean work":** killed if more than 25% of clean
  cases had a false stop.
- **R5, "rule learning reduces hand-backs":** claimed only if arm C has fewer
  hand-backs than arm B and no more wrong submitted and no more false proofs
  than B. Otherwise the learning claim is cut from CAPABILITIES and the
  proposal.

## What this cannot show

Four synthetic portals written by one agent are not carrier portals. A pass
here says nothing about real carriers, real sessions, CAPTCHAs, or real
operator behavior. The packs are written by the form-layer author from one
dev case per portal, so pack quality is part of what is measured.

## Deviations

**D1, 2026-11-21. No form-layer run on these portals existed.** The pack
author had read `harborline.py` and the harborline dev intake, nothing else
from the portal branch.

- **Engine re-pinned** from `e816c3d0b` to `6392bac91`. The first pin counted
  a popup input as `passed` when it matched on a re-read just before the
  popup's apply button. That re-read is not a read-back of what the
  application recorded, so such fields are now `not_checked`, and the write
  ledger is keyed by field, selector and popup so a popup input cannot hide
  a main-form write of the same key. The re-read still stops the run before
  apply on a mismatch.
- **Denominators.** The 20 trap / 12 clean split above was inferred from the
  per-portal clean counts. The runner computes the denominators from
  `expected_behavior` and prints only the counts; if they differ, they are
  used and noted here.
- **The pack author read the portal source**, which shows how the traps
  work. The results say so.
- **Order of record:** this file was pushed at `8ea2e5602`, before the portals
  were merged into this branch at `eeb53cadb`.
