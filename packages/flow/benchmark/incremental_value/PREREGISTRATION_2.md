# Pre-registration, run 2: what OpenAdapt adds on top of a competent agent

Run 1 ([`PREREGISTRATION.md`](PREREGISTRATION.md), results in [`README.md`](README.md)) found that B does not win. This is a second, separately pre-registered run. This file is committed before any run-2 item runs, and before B's run-2 contract is calibrated. B's contract is frozen by sha256 before anyone reads the run-2 fault set. Nothing here changes after the first run-2 item starts. Anything that has to change later is added as a dated deviation.

## What changed since run 1, and why

Run 1's root causes (README, "Why B missed, stopped or duplicated") and its adversarial review led to these fixes. Each landed as its own reviewed PR on `main`, with tests against a real OpenEMR:

- **Late writes (product).** The frame can recheck the whole database at a time the contract declares (`frame.database.recheck_after_s`). Later writes by the same actor are excused through their receipts. PR: RECHECK_PR.
- **Writes that touch no patient (product).** The frame can attribute another user's change to a row that user owns, such as their own settings, by owner, table and time. PR: OWNER_PR.
- **Uncertain writes across a restart (product).** A durable write ledger, #1628, replaces the harness's in-memory count of sent saves. B reads it on restart, as a real adapter would.
- **Stops leave nothing undone (product).** Compensating rollback, #1634, if it is on `main` at the pinned commit. If it is not, B stops without undoing, as in run 1, and this file says so at the freeze.
- **Oracle and harness (benchmark).** PR: HARNESS_PR.
  - The oracle compares every new-chart column the referral does not name against the values a clean save leaves. These are recorded on calibration saves outside the stream (`calibrate.py --record-form-defaults`).
  - It excuses a colleague's legitimate rows on the new chart.
  - A fault that changes nothing (`ctx.fault_write` moves none of its tables) makes its item "not run": excluded and reported.
  - B's audit view also covers log entries that name no patient, and it parses `REPLACE INTO`.

No fix was tuned on run 1's fault set to pass it. None changes a threshold. The run-1 set is now known, so it does not decide run 2.

## Arms

As in run 1, except where stated.

- **A.** #1528's `agent_tools` agent (`claude-sonnet-5-5`, effort medium, 40 turns, $0.50 per item), with REST read-back and a duplicate check. Its prompt and tools are unchanged.
- **B.** The same agent, with its writes going through `openadapt_flow.verifier` and these checks:
  - the record check over REST, and the whole-database frame over MariaDB, using the OpenEMR app profile, audit attribution and the `history_data` side record;
  - the #1609 hold;
  - the durable ledger (#1628);
  - the late recheck at `recheck_after_s = 240`.

  **Why 240 s:** OpenEMR 8.0.0.3's shortest default interval for an active background service is 2 minutes (`Email_Service`, `sql/database.sql` at tag `v8_0_0_3`). A job the save triggers runs at that service's next tick, at most one interval later. The second interval allows for that run finishing. 240 s is two intervals.

  **Limitation, stated now:** the fault brief caps a delayed write at 90 s, so this run cannot show whether 240 s is long enough in production.
- **The recheck holds up B's next item (blocking).** After item k, B waits until its recheck is due, runs it, and only then starts item k+1. B's verdict on item k becomes the recheck's outcome. A late REFUTED or UNCERTAIN counts as a stop at that time.
  - **Why blocking:** run non-blocking, item k's recheck overlaps items k+1 to k+4. If one of those is a fault that B catches, the product correctly returns UNCERTAIN for item k, because it can't tell whose write caused the harm. With faults making up 27% of the stream, that would charge B false stops for a fault density no clinic sees.
  - **Known limitation, stated now:** in production, writes overlap and those disputes happen. The non-blocking slice below measures how often.

## Workload

- **N = 300 items per arm,** in the same order for both arms, with seed 2029. This is run 1's mix at 300:
  - 168 ordinary referrals;
  - 80 blind fault cases;
  - 52 harness perturbations: 13 each of delayed legitimate write, restart after the save, concurrent staff edit, and timeout after a successful save.
- **Blind fault set 7.** `faultset7.yaml`, 80 cases: 60 harmful and 20 clean.
  - sha256: `372050810443e086c9d775ece235f61efa4e2bb8d0e74708698c175aaf44e3a9`.
  - It is frozen in openadapt-internal at `qualification/incremental-value-2/`, commit b617a2a.
  - A separate agent wrote it from [`FAULT_BRIEF_2.md`](FAULT_BRIEF_2.md): run 1's brief with the earlier set, its results and this harness put out of bounds, and 80 cases asked for. That agent read nothing else.
  - The set stays private. Only its hash, its class counts and its results by class are published.
  - It is never reused as a held-out set, and neither are sets 1 to 6.
- **Fault ops.** These are the implementations of the cases. They are written only after B's run-2 contract sha256 is committed. Each case is implemented as written, or reported "not run" with the reason.

- **The non-blocking slice (secondary, reported apart).** 30 items, built with seed 2030 at run 1's density:
  - 17 ordinary referrals;
  - 8 fault cases drawn by that seed from faultset 7;
  - 5 perturbations.

  B only, with rechecks run at item boundaries while later items proceed (`INCREMENTAL_VALUE_RECHECK_MODE=boundary`). The slice reports:
  - how many rechecks came back UNCERTAIN because a later write's window was disputed;
  - B's false stops in the slice.

  It is not part of the primary result or the verdict. Fault cases may appear in both the slice and the main stream, and are scored separately in each.

## Schedule

- **One fixture lease at a time, in chunks.** The 300 items run in 6 chunks of 50 per arm, by item order (iv-001–050, 051–100, and so on). The lease is released between chunks so queued runs can go in between.
- **Chunk order, the same for both arms:** B1, A1, B2, A2, B3, A3, B4, A4, B5, A5, B6, A6, then the slice. Interleaving the arms keeps time-of-day drift from favouring either one.
- **The fixture between chunks.** It is not reset between chunks; each arm's chunk resumes from its own ledger. Chunk boundaries are fixed here and are not moved.

## Oracle and outcomes

As in run 1, with the fixes above. The outcome classes are run 1's, plus two:

- **not run:** excluded and reported. Either the harness failed, or a harmful fault never landed.
- **late REFUTED or UNCERTAIN:** treated as a stop.

## Measures

The same as run 1:

- correct completions, missed harmful changes, caught harmful changes, false stops and duplicates;
- people minutes per correct transaction;
- model cost per item;
- **latency.** B's 240 s recheck wait is counted three ways, all declared now and all reported, none chosen afterwards:
  1. **People minutes do not include it.** No person waits for a recheck.
  2. **Recheck latency, on its own:** the time from B's first verdict to the recheck's verdict, median and p90.
  3. **Wall time per item without the wait:** the item's wall time minus the time spent waiting for its recheck to fall due, median and p90, for both arms.

  B's wall time with the wait is reported too, as a raw figure.

People time is costed from [`costs_2.json`](costs_2.json), which is frozen with this file:

- **A missed harmful change: 14,737 s.** This is **cited, not modelled.** Correcting one duplicate patient record costs USD 96 (Just BH, Marc D, Munns M, Sandefer R, "Why Patient Matching Is a Challenge: Research on Master Patient Index (MPI) Data Discrepancies in Key Identifying Fields", *Perspectives in Health Information Management* 13 (Spring 2016) 1e, PMID 27134610, citing Children's Medical Center Dallas). At the US median wage of a medical records specialist, USD 23.45 an hour (BLS Occupational Employment and Wages, May 2023, 29-2072), that is 4.09 hours.
  - It is applied to every missed harmful change, on the stated assumption that correcting a wrong value or a collateral change costs at least what correcting a duplicate costs.
  - **Sensitivity:**
    - low: run 1's modelled cleanup, 330 s;
    - high: USD 1,000 per duplicate pair (HIMSS 2014 panel, as reported by Healthcare IT News), 153,518 s.
- **A caught harmful change: 420 s,** modelled. That is stop 90 + reconcile 120 + redo 180 + interrupt 30, from cost model V1. No operator review was timed, so this is not measured.
- **A false stop or a correct stop: 120 s,** modelled: stop 90 + interrupt 30. This corrects run 1's scorer, which also charged reconcile (review finding 5).
- **A failed stop: 420 s.** **A silent absence: 330 s.** Both modelled, as in `costs_2.json`.
- **Setup: A 60 and B 90 engineer-minutes,** each charged once over the whole run of N items. A secondary figure, declared now, amortizes setup over 1,000 items. The verdict uses the primary figure.
- **×0.5 and ×2** of every per-outcome figure, plus the low and high missed-harm figures, are reported. The verdict uses ×1.

## The win, decided now

The same three criteria as run 1. B wins only if all three hold over the whole stream of N scored items:

1. B has **fewer missed harmful changes** than A. It must be strictly fewer; a tie is a loss.
2. B's **false stops ≤ A's false stops + ⌈N/100⌉.**
3. B's **people minutes per correct transaction < A's,** at ×1 with `costs_2.json`.

Otherwise the result is "B does not win", and it is published as such.

## Pins and run rules

- **Engine:** the `main` commit ENGINE_COMMIT, with every fix above merged. Both arms run on it.
- **Fixture:** OpenEMR 8.0.0.3 (`openemr/openemr:8.0.0.3@sha256:0aa4d3d5…`) with MariaDB 11.8, from the pinned lock in `benchmark/openemr_local`. Two copies run, one per arm, with A and B interleaved in time. The lease is taken through `~/oa/scratch/fixture-lease/QUEUE`.
- **Host load:** the between-item gate is 60. The load is recorded per item at its start and end. Timings are reported both raw and for items run under load 40.
- **Model spend:** under $150 in all, calibration included.
- **Retries:** each item is retried at most once, and only for an infrastructure error. Every retry is reported.
- **B's contract:** `contract_2.yaml` with `audit_query_2.sql`. It is calibrated on clean referrals outside the stream, and the harness's own legitimate writes. Its sha256 is committed before the fault ops are written or the fault set is read.
