# Pre-registration: the reworked form layer on two new held-out quote portals

Written 2026-11-21, before anyone on the form-layer side opened the new
portals (code, cases, intakes or answer key), and before any run on them.
Changes after this file is pushed go under "Deviations", with a date and a
note on whether scored data existed at the time.

## Question

The second held-out run (`../form_tx_heldout2_run/RESULTS.md`) failed K2,
K3 and K4 and named five causes: a sign-in in the middle resets earlier
answers that the layer does not re-check; controls no binding covers are not
checked at all (which also allowed two false receipts on the dev set); stops
carry no field label, or name a radio option instead of its group; a lookup
list with several identical "Select" buttons always stops; and read-back
rarely reaches `verified_done`. The rework (`claude/form-tx-rework`, onto
`claude/form-gaps-integration`) addresses each as a general bug class. On two
quote portals the layer was not built against, how often does it stop a trap
before submit, stop clean work, submit a wrong value, and end with a
verified receipt?

## What is frozen

- **Engine:** OpenAdapt branch `claude/form-tx-rework` at the commit in
  `ENGINE_SHA` next to this file (`8a9f08b75`), on top of
  `claude/form-gaps-integration` at `72a6ad428` (the engine of the second
  run). The freeze covers `packages/flow/openadapt_flow/`.
- **Portals and answer key:** branch `claude/form-tx-heldout3-portals` at
  `177378a6e3c9e26a72069ea217b8c68e1f0b5e55`, written by a fresh agent that
  was told not to read the form layer, its runs, packs or results, and that
  reported back only the branch, the hash, the answer key's sha256 and the
  counts. `answer_key.json` sha256:
  `c493815440bc9e938aa63f9028e991a8e72d70b4388957a9196d18dc3f05c4d1`.
  Two portals (`corvane`, `pellbrook`), one dev case each, 16 scored cases
  (reported: 7 `submit_clean`, 9 trap). The runner computes denominators from
  `expected_behavior`, and those counts are the ones used.
- **Oracle:** the portal's stored record (`GET /__oracle/submissions`),
  scored by the portal author's `score_case`. The layer never sees it.

## Procedure

1. Push this file and `ENGINE_SHA`. Only then merge the portals branch into
   the run branch.
2. **Demonstrations.** For each portal, the portal author's careful-human
   driver runs on the dev case only, and before each click the recorder saves
   what the layer's surface sees (`record_demo.py`, as in the second run).
3. **Packs from the dev case only.** Each pack is written by a separate,
   fresh agent that gets only the dev intake, the dev-case demonstration, the
   dev-case run output and the pack format (`openadapt_flow/form_tx/types.py`
   and one pack from the second run as an example). It does not read portal
   source, drivers, scored intakes or the answer key. Packs are data and are
   committed before the first scored case.
4. **Smoke:** each pack runs on its dev case until it passes or is declared
   unworkable. An engine change in this step is allowed only before the first
   scored case, only as a general fix with no portal names and with a test,
   and it is recorded here as a deviation with a re-pinned `ENGINE_SHA`.
5. **Scored run:** every scored case runs once per arm, in `spec.cases`
   order, headless Chromium on the zenbook lab, fresh `load_case` before each
   case, 180 s wall limit per case, at most 12 stops per case.
6. Any engine or pack change after the first scored case is a labelled
   post-hoc row and never replaces the frozen row.

## Arms

- **A, unguarded:** the portal author's `naive_filler.py`, run once
  unchanged.
- **B, form layer, learning off.**

## Scripted operator (arm B)

Unchanged from the second run (`../form_tx_heldout_run/run.py`): on each stop
the operator looks up `operator_script` by the stop's field label, then by its
dossier key, and takes that action; on a sign-in stop it signs in with the
case's `session_login` and marks the stop fixed; with no entry it escalates,
which ends the case unsubmitted. A `provide_value` of an empty string on a
prefill stop clears the field. On a `fields_not_rechecked` stop the script's
`provide_value` is not offered, so with no control to type into the operator
escalates. Every stop shown to the operator counts as one hand-back.

## Metrics (arm B unless stated; denominators from the answer key)

Identical to the second run's pre-registration: correct submissions (and
correct and `verified_done` with a receipt); wrong submitted, split by outcome
(`verified_done` is a false proof; `readback_refuted` or
`readback_incomplete` is flagged after submit; anything else is silent); trap
cases stopped before submit; clean cases with a false stop (over all clean
cases, and over the clean cases reached); verified share of submitted cases;
fields `not_checked`; cases ending `readback_incomplete`; hand-backs;
unsubmitted cases; errors; reached cases per portal. New in this run, reported
but not scored: for each wrong submission, whether the flag named the wrong
field (`not_checked` or `failed` includes it, or an `unbound:` entry for its
control).

## Kill or rework criteria (fixed now, new portals, arm B)

| | Criterion | Bar |
| --- | --- | --- |
| K1 | Trap cases stopped before submit | at least 75% |
| K2 | Clean cases with a false stop (registered denominator) | at most 10% |
| K3 | Submitted quotes that end `verified_done` with a receipt | at least 50% |
| K4 | Wrong values submitted | zero |

- **Kill:** any wrong value submitted with `verified_done` kills the claim
  that a receipt means the quote is right. This holds even when the receipt
  lists the wrong field's control under `not_covered`. Any wrong value
  submitted silently kills the claim that nothing wrong goes through
  unflagged.
- **Keep:** K1 to K4 all met. **Rework:** otherwise, each failed line named
  with its cases.
- **Companion to K1:** trap cases stopped before submit and then submitted
  correctly, and per portal whether every case stopped.
- **What `verified_done` means here:** every bound or written field passed
  read-back, and no control outside the pack's bindings held a value the run
  did not set, other than pack-declared unbound controls at their declared
  blank value. The receipt lists the keys it covers (`covered_keys`) and what
  it does not (`not_covered`). A receipt with entries in `not_covered` still
  counts as `verified_done`; it does not relax the kill line above.

## Known limits stated in advance

- **Small n.** 16 scored cases on two portals by one author. One case moves
  K2 by about 14 points (7 clean cases) and K3 by about 6 to 10. A pass or a
  fail here is weak evidence either way; the results give counts, not only
  percentages.
- The founder's popup rule stands: a popup input matched before apply is
  still `not_checked` unless the stored summary shows it.
- A trap the operator script does not cover ends escalated even when the
  stop is correct, so the K1 companion is the fairer view of usefulness.

## What this cannot show

Two synthetic portals are not carrier portals. Nothing here speaks to real
carriers, real sessions, CAPTCHAs or real operators. Pack quality is part of
what is measured: every pack is written from one dev case.

## Deviations

**D1, 2026-11-21. Engine re-pinned before any scored case.** `ENGINE_SHA`
moves from `8a9f08b75` to `13772e2b6`. The only engine change is
`33851c2b9`; the later commit touches docs only. The change came from the
dev re-run of the eight known portals with their old packs, and from the
pellbrook pack author's dev-case smoke. Every part is a general fix with a
test and no portal names:
- an intake value that no binding enters, and that the pack does not list in
  `ignored_intake_keys`, keeps a run from `verified_done` (`intake:<key>`
  `not_checked`). This is the class behind a false receipt that recurred on
  the dev set, an additional-driver list the pack never entered;
- a radio group with no legend is named by its table row's header cell;
- a validation marker counts only when it is added to a row's old text;
- a screen's "did it advance" signature includes the visible field labels,
  so a form that asks one question at a time in one control advances (the
  pellbrook dev case stopped at its second question without it).

Both pack authors then updated their packs for the first rule (corvane
lists 4 keys, pellbrook 2, each with a reason in the module), and both dev
cases end `verified_done` and correct at `13772e2b6`. Departures from the
read limits, as the authors reported them: the pellbrook author ran one
`grep` in `playwright_surface.py` (the engine, not the portal), and both
authors ran extra dev-case diagnostics (a dev run with two bindings removed;
snapshot hooks on the dev case) to see blank-form defaults that the
demonstration did not capture. Neither read portal source, scored intakes or
the answer key. Packs are frozen at the commit that adds them, before the
first scored case.
