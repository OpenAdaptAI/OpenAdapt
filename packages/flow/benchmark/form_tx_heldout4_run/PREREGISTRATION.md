# Pre-registration: Set 4, pack compiler and round-3 fixes on three new portals

Written 2026-11-21, before anyone on the form-layer side opened the new
portals (code, cases, intakes or answer key), and before any run on them.
Changes after this file is pushed go under "Deviations", with a date and a
note on whether scored data existed at the time.

## Question

The third held-out run (`../form_tx_heldout3_run/RESULTS.md`) lost most cases to
five causes:
- false stops on UI defaults;
- stops that could not name their field;
- identical Select rows;
- popups the dev case never opened;
- one false receipt, on a bound field shown with no intake value.

Round 3 adds, as general mechanisms with no portal names:
- a fail-closed stop for a bound field shown with no intake value (unless the
  pack marks it `optional`);
- a pre-submit comparison against the review page;
- a strict type-ahead pick, where a pick counts only when the click changed the
  field and a leading part never matches;
- coverage by subject, with no verdict meaning `not_checked`;
- a setup-time pack compiler from dev-case demonstrations
  (`openadapt_flow/form_tx/demo_compiler.py`).

The compiler produces a defaults table, row key cells, option codecs,
unopened launchers, and error routes (suggest-only).

On three portals neither the layer nor the compiler was built against:
1. Do these deterministic fixes halve false stops on clean cases compared with
   the round-2 engine and packs written without the compiler?
2. Do they do it without a false receipt or a silent wrong submit?

## What is frozen

- **Portals and answer key:** branch `claude/form-tx-heldout4-portals` at
  `b4180ba79ed71ef6e9d718e6cf6076dbe183f2f3`, written by a fresh agent that
  was told not to read the form layer, its runs, packs, results or the other
  held-out sets. It reported back only the branch, the hash, the answer key's
  sha256 and the counts. `answer_key.json` sha256:
  `7fe904358bf0db41cbeae756cc776cc2a90fdf7f90c5128c8344103e8560f578`.
  - Three portals: `wrenfield`, `corvane` and `thistlebrook`, one dev case
    each, 32 scored cases (reported: 15 `submit_clean`, 17 trap).
  - This `corvane` is a different portal from set 3's `corvane`; the portal
    author picked the name independently.
- **Engines:**
  - B0 runs `claude/form-tx-rework` at `866aac8c1`, the engine before round 3.
  - B runs the round-3 engine at the commit in `ENGINE_SHA_B`, set and pushed
    before the first scored case (see D-rules below).
  - Both freezes cover `packages/flow/openadapt_flow/`.
- **Oracle:** the portal's stored record, scored by the portal author's
  `score_case`.

## Procedure

1. Push this file. Only then merge the portals branch into the run branch.
2. **Demonstrations.** The portal author's careful-human driver runs on each
   dev case only. Before every click and every value change (fill, select,
   check, type), the recorder saves what the surface sees
   (`record_demo.py`), so the first sighting of each control is its
   blank-form value.
   - Only one dev case exists per portal, so every pack uses exactly
     **one demonstration** plus this blank-form probe.
   - The defaults table therefore proves a default only where the dev intake
     asked for a value other than the one the blank form showed. T1 cases
     whose default equals the dev intake value stay unproven.
3. **Packs.** Each pack is written by a fresh agent that gets only the dev
   intake, the dev demonstration, its own dev-case run output, the pack
   format, and one earlier pack as an example.
   - **B0 packs:** 3 agents. They do not get the compiler report and must not
     use the round-3 pack fields: `optional`, `review_readbacks`,
     `ignored_intake_keys` beyond what the B0 engine supports, and the
     compiler.
   - **B packs:** 3 different agents. They also get the compiler's report for
     their portal and may use every engine feature at `ENGINE_SHA_B`.
   - No author reads portal source, drivers, scored intakes or the answer key.
   - Packs are committed before the first scored case.
4. **Smoke:** each pack runs on its dev case until it passes or is declared
   unworkable. An engine change for B is allowed only before the first scored
   case, as a general fix with a test and no portal names, recorded here as a
   deviation that re-pins `ENGINE_SHA_B`. The B0 engine never changes.
5. **Scored run:** every scored case runs once per arm, in `spec.cases`
   order, headless Chromium on the zenbook lab, with a fresh `load_case`
   before each case, a 180 s wall limit per case and at most 12 stops per
   case.
   - Arm order: A, B0, B.
   - Per-control logs (`run.py --control-log`) are kept for B0 and B.
6. Any change after the first scored case is a labelled post-hoc row and never
   replaces a frozen row.

## Arms

- **A, unguarded:** the portal author's `naive_filler.py`, run once.
- **B0:** round-2 engine, packs without the compiler.
- **B:** round-3 engine, packs with the compiler report.

The scripted operator is unchanged from the earlier runs (`run.py`).

## Metrics (denominators from the answer key)

These are the same as in the earlier runs, per arm, per portal and pooled:
- correct submissions, and correct and `verified_done` with a receipt;
- wrong submitted, split into false proof, flagged after submit, and silent;
- trap cases stopped before submit, and stopped then correct;
- clean cases with a false stop;
- verified share;
- `not_checked` fields;
- hand-backs;
- unsubmitted cases;
- errors.

**Reported by type.** Wrong submits, clean-case false stops and `not_checked`
fields are each listed with a cause type, from the screen-verifier taxonomy:
- T1, UI default versus stored prefill;
- T2, a control the demo skipped;
- T3, identical Select rows;
- T4, a popup the demo never opened;
- T5, a look-alike option;
- T6, which field an error belongs to;
- T8, a stale prefill;
- other.

The cause type is assigned after the run by the form-layer author from the
rows and per-control logs. That is a judgement and is labelled as one. For
each pack the results also state how many demonstrations it used, which is
one, and which compiler findings it adopted.

## Criteria (fixed now)

- **Halving test (kill):** B's clean-case false stops must be at most half of
  B0's, counted over all 15 clean cases. If B0 has no clean false stop, the
  test cannot be met and is reported as not applicable. If B misses it, the
  deterministic-fix claim is killed.
- **Hard gates for B:**
  - zero false receipts, meaning a wrong value submitted with `verified_done`;
  - zero silent wrong submits.

  Either one failing kills the receipt claim or the nothing-unflagged claim,
  whatever the halving test says.
- **K1 to K4 for B**, with the same bars as before (K1 at least 75%, K2 at
  most 10%, K3 at least 50%, K4 zero), reported next to B0's.
- **Small n:** 32 cases, 15 of them clean. One clean case is about 7 points
  of K2.

## What this cannot show

Three synthetic portals by one author, each pack built from one
demonstration. Nothing here speaks to real carriers, real sessions or real
operators.

## Deviations

None yet.
