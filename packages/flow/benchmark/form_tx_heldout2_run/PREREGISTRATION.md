# Pre-registration: the form layer after its gap fixes, on new held-out quote portals

Written 2026-11-21, before the form-layer author opened anything from the new
portals (code, cases, intakes or answer key), and before any run on them.
Changes after this file is pushed go under "Deviations", with a date and a
note on whether scored data existed at the time.

## Question

The first held-out run (`../form_tx_heldout_run/RESULTS.md`) found six engine
gaps. Five pull requests close them: text-only summary read-back (#2449),
typed type-aheads and surface errors as stops (#2448), iframe and shadow-DOM
forms (#2440), two-step and in-page popups (#2441), and an expiry banner as a
sign-in wall plus unchecked radios as empty (#2447). With those in, on four
new quote portals the layer was not built against, how often does it stop a
trap before submit, stop clean work, submit a wrong value, and end with a
verified receipt?

## What is frozen

- **Engine:** OpenAdapt branch `claude/form-gaps-integration` at the commit in
  `ENGINE_SHA` next to this file (`335c322ec`): `claude/form-tx-weekend`
  (#2429) with the five PRs merged at #2449 `4a156209b`, #2448 `9a0cd7977`,
  #2440 `4add17832`, #2441 `374563a32` and #2447 `950a75dce`. Those heads
  include fixes found while writing packs for the old (dev-set) portals:
  checkboxes and radios set by click and re-read, including on pages that
  redraw them with new ids and names; ARIA radios labelled by their own text;
  "Select one" read as empty; a redrawn type-ahead found again before its
  pick is checked; and a popup's displayed result checked against the value
  it hands back (`result_anchor`). One change exists only on the integration
  branch, because it needs both #2449 and #2441: a popup's displayed result
  can be read from page text. The freeze covers `packages/flow/openadapt_flow/`.
- **Portals and answer key:** branch `claude/form-tx-heldout2-portals` at
  `d2d6883374d9984b0a14d6ce6a603ebfd4cdf8dc`, written by a separate agent
  that was told not to read the form layer, and that reported back only the
  branch, the hash and the counts. `answer_key.json` sha256:
  `46df94bf3da9005278d14bb59ee8c9a5b7784ce47d11178532de11fc42c70f90`.
  Four portals, one dev case each, 33 scored cases (reported: 18 trap,
  15 clean). The runner computes the denominators from `expected_behavior`
  and those counts are the ones used.
- **Oracle:** the portal's stored record (`GET /__oracle/submissions`), scored
  by the portal author's `score_case`. The layer never sees the oracle.
- **The old four portals are a dev set.** Their code and answer key are
  public, and the gap fixes were written against what they showed. They are
  rerun with the same frozen engine and reported in their own table, labelled
  as a dev set. No claim rests on them.

## Procedure

1. Push this file and `ENGINE_SHA`. Only then merge the portals branch into
   the run branch.
2. **Packs from the dev case only.** For each new portal, the pack author
   reads the dev case intake and records one demonstration: the portal
   author's careful-human driver runs on the dev case only, and before each
   of its clicks the runner saves what the layer's surface sees (controls,
   labels, page-text pairs, frames). The pack author does not read portal
   source, drivers, scored intakes or the answer key. Packs are data and are
   committed before the first scored case.
3. **Smoke:** each pack runs on its dev case until it passes or is declared
   unworkable. An engine change in this step is allowed only before the first
   scored case, only as a general fix with no portal names and with a test,
   and it is recorded here as a deviation with a re-pinned `ENGINE_SHA`.
4. **Scored run:** every scored case runs once per arm, in `spec.cases`
   order, headless Chromium on the zenbook lab, fresh `load_case` before each
   case, 180 s wall limit per case, at most 12 stops per case.
5. Any engine or pack change after the first scored case is a labelled
   post-hoc row and never replaces the frozen row.

## Arms

- **A, unguarded:** the portal author's `naive_filler.py`, re-run once
  unchanged.
- **B, form layer, learning off.**
- Arm C (rule learning) is dropped: the first run cut the learning claim
  (R5), and nothing in the fixes changes learning.

## Scripted operator (arm B)

On each stop the operator looks up `operator_script` in the answer key, by
the stop's field label, then by its dossier key, and takes that action. On a
sign-in stop (`auth_wall_detected`) it signs in with the case's
`session_login` and marks the stop fixed on screen. With no script entry it
escalates to the owner, which ends the case unsubmitted. The operator does
**not** run any estimator by hand (the first run's deviation D2 is gone). Every
stop shown to the operator counts as one hand-back.

## Metrics (arm B unless stated; denominators from the answer key)

- **Correct submissions:** `score_case(...).correct`; also correct and
  `verified_done` with a receipt.
- **Wrong submitted:** submitted with `record_correct` false, split by outcome:
  `verified_done` (a false proof), `readback_refuted` or `readback_incomplete`
  (flagged after submit), anything else (silent).
- **Trap cases stopped before submit:** over the trap cases
  (`must_stop_before_submit` or `needs_operator_answer`), cases with at least
  one stop before the first submission. A trap case the layer never reached is
  counted as not stopped.
- **Clean-case false stops:** over the clean cases (`submit_clean`), cases with
  at least one stop before submission. Reported two ways: over all clean cases
  (the registered figure) and over the clean cases the layer reached, meaning
  it submitted or stopped on the portal's form (a run that errors on its first
  page has not reached it).
- **Verified share:** of submitted cases, the share ending `verified_done` with
  a receipt.
- **Fields `not_checked`**, cases ending `readback_incomplete`, operator
  hand-backs, unsubmitted cases, runs that raised an error or got stuck.
- **Reached:** per portal, cases where the layer submitted or stopped on the
  form, so a portal it cannot operate is visible as such.

Every number is reported per portal and pooled. No case is dropped.

## Kill or rework criteria (fixed now, new portals, arm B)

| | Criterion | Bar |
| --- | --- | --- |
| K1 | Trap cases stopped before submit | at least 75% |
| K2 | Clean cases with a false stop (registered denominator) | at most 10% |
| K3 | Submitted quotes that end `verified_done` with a receipt | at least 50% |
| K4 | Wrong values submitted | zero |

- **Kill:** any wrong value submitted with `verified_done` (a false receipt)
  kills the claim that a receipt means the quote is right. Any wrong value
  submitted silently (not a stop, not `readback_refuted`, not
  `readback_incomplete`) kills the claim that nothing wrong goes through
  unflagged.
- **Keep:** K1 to K4 all met.
- **Rework:** otherwise. Each failed line is named, with the cases behind it.
- If the reached-only false-stop rate is above 10% while K2 passes, the
  results say so next to K2.

## Known limit stated in advance

The founder's rule stands: a popup input that matched on a re-read before its
apply button is still `not_checked`, because a re-read of the popup is not a
read-back of what the application recorded. Any case that types into a popup
estimator therefore ends `readback_incomplete`, not verified, unless the
stored summary shows that input. This may decide K3. The results report how
many submitted cases were held below verified by popup inputs alone.

## What this cannot show

Four synthetic portals by one agent are not carrier portals. A pass says
nothing about real carriers, real sessions, CAPTCHAs or real operators. The
packs are written by the layer's author from one dev case per portal, so pack
quality is part of what is measured.

## Deviations

None yet.
