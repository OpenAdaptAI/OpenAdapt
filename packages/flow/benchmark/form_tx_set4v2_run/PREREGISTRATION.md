# Pre-registration: Set 4 v2 (sealed), pack compiler and round-3 fixes on three new portals

Written 2026-11-22, after the v2 portals were sealed. Nobody on the
form-layer side has read them (code, scored intakes or answer key), and
nothing has run on them yet.

**Why v2.** The first Set 4 (`../form_tx_heldout4_run/`) is no longer held
out, for three reasons:
- its plaintext answer key sat in a shared worktree;
- portals in that tree were edited after some study arms had results;
- two portals overlap a renderer's layouts used by another study.

It is now a dev set: its demos and B0 packs stay as dev material, and no
claim rests on them.

**What is sealed.** A fresh agent that never read the form layer built three
new portals:
- **Location:** outside every worktree, in `~/oa/private/form-tx-set4v2/`,
  read-only, with local git only. Freeze commit `eb7189f83`.
- **Answer key:** encrypted (`answer_key.json.enc`, sha256 `7484eb70...7e817d`;
  plaintext sha256 `ad43d211...0c712ed9`). The key sits in a separate 0600
  file that only the scorer reads.
- **Case source:** encrypted the same way.
- **Pack authors** see only the public subset (`~/oa/private/form-tx-set4v2-public/`):
  dev intakes, the vocabulary and portal metadata.
- **Scored cases:** 30 scored cases (14 `submit_clean`) on `brackwater`, `larkspan`
  and `hollowfen`.

Everything below keeps the first Set 4's rules, with the portal set and
paths replaced.

## What is frozen

- **Portals and answer key:** the sealed folder above, freeze commit
  `eb7189f83a77e80515923d9ff03be1e3bc8e1ca4`. The answer key's plaintext
  sha256 is `ad43d211160b08f681afe016d8784187fab73c715ffbfeeb1eb668310c712ed9`
  and its encrypted sha256 is
  `7484eb70c49f1fcec190ac717fb40b095a356fd36f1e66ab5544f1c27a7e817d`.
  - Three portals: `brackwater`, `larkspan` and `hollowfen`, one dev case
    each.
  - 30 scored cases (14 `submit_clean`, 16 trap).
- **Engines:**
  - B0 runs `claude/form-tx-rework` at `866aac8c1`, the engine before round 3.
  - B runs `claude/form-tx-rework` at `1ddd6971284f27f0dcdbe86051d10147e6a87e91`
    (in `ENGINE_SHA_B`), the round-3 head. It adds the rulings of 2026-11-21:
    intake values the portal never asks for are not covered unless the
    pack requires them, and a radio group is one subject.
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

**Secondary metric (declared under D1, before any scored case).** For every
case B ends `verified_done`, the result records which read produced it:
- the page the run landed on after commit (`same_screen`);
- a new navigation or search that fetched the record from the server again
  (`fresh_navigation`);
- a separate session.

The engine's `SummaryVerdict.read_route` holds this; it is reporting only.
Set 4's verified share is reported both ways:
- under today's meaning;
- under the shared check-level ladder, where a same-screen read counts as
  SHOWN and only a fresh reload, a separate session or the system of record
  counts as VERIFIED.

## Criteria (fixed now)

- **Halving test (kill):** B's clean-case false stops must be at most half of
  B0's, counted over all 14 clean cases. If B0 has no clean false stop, the
  test cannot be met and is reported as not applicable. If B misses it, the
  deterministic-fix claim is killed.
- **Hard gates for B:**
  - zero false receipts, meaning a wrong value submitted with `verified_done`;
  - zero silent wrong submits.

  Either one failing kills the receipt claim or the nothing-unflagged claim,
  whatever the halving test says.
- **K1 to K4 for B**, with the same bars as before (K1 at least 75%, K2 at
  most 10%, K3 at least 50%, K4 zero), reported next to B0's.
- **Small n:** 30 cases, 14 of them clean. One clean case is about 7 points
  of K2.

## What this cannot show

Three synthetic portals by one author, each pack built from one
demonstration. Nothing here speaks to real carriers, real sessions or real
operators.

## Deviations

**D1, 2026-11-22, packs written; no scored case had run.**
- **Engines.** B0 packs were written and smoked in a worktree whose
  `openadapt_flow` equals `866aac8c1` (the compiler module removed). B packs
  were written at `1ddd69712`.
- **Read-limit departures, as the six authors reported them:**
  - Several used the runner's `--control-log` on their own dev runs, which
    shows the dev page text and the last-seen controls.
  - One B author read parts of `reconciler.py` and grepped
    `playwright_surface.py`. Others grepped engine files for a few lines, or
    listed the run folder's file names.
  - One B0 author read part of a set-4 (first-version) dev pack for another
    portal.
  - No author opened the sealed folder, the key, scored intakes or the
    answer key.
- **Pack choices to review, recorded here and not changed:**
  - Packs tick review-page confirmation boxes from a derived fact (an
    attestation, not intake data).
  - Two brackwater packs narrow the modal selectors to exclude the portal's
    section dialogs; any other open dialog still stops.
  - Some packs sum intake items in `derived_facts` where the vocabulary
    defines the total that way.
  - One hollowfen pack declares its post-submit read-back once per step, to
    meet the per-screen write ledger.
- **Dev smoke.** No v2 dev case reached `verified_done` in either arm:
  - larkspan and hollowfen submit correct records but end
    `readback_incomplete`: a combined summary row, an unreadable
    lookup-window result, and attestation boxes;
  - brackwater stops before submit on a type-ahead whose directory entry
    cannot be built from the intake.

  The registered criteria are unchanged.
