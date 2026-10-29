# What OpenAdapt adds on top of a competent agent (live OpenEMR)

**Result: B does not win.** It meets the first two pre-registered criteria and fails the third:

1. **Missed harmful changes: B 4, A 13.** Met.
2. **False stops: B 3, A 5.** The allowance was A + 1. Met.
3. **People minutes per correct transaction: B 2.31, A 2.11.** These minutes are modelled. Not met.

People time is modelled with cost model V1; no operator was timed. The failure on criterion 3 is close, and it comes from how the cost model charges a stop. A stopped write costs a person more modelled time (420 s) than a missed harm's later cleanup (330 s). B also adds 30 engineer-minutes of contract setup over A's 60. Leaving setup out, B takes fewer modelled minutes (1.25 against A's 1.32). The pre-registered rule counts all people time, setup included, so the verdict stands. It is the same at ×0.5 and ×2 of the per-touch numbers.

Both arms ran the same 90 items on live OpenEMR on 2026-10-28. One fault case is excluded from both arms (see deviation 5), which leaves N = 89.

## Results

| | A: agent with read-back | B: agent + OpenAdapt |
|---|---:|---:|
| Correct completions | 70 | 72 |
| Correct once the operator has handled what reached them | 76 | 85 |
| Missed harmful changes | **13** | **4** |
| Harmful changes caught (stopped) | 1 | 10 |
| False stops | 5 | 3 |
| Correct stops (the end state needed none) | 1 | 1 |
| Duplicates (more than one chart for the person) | 1 | 1 |
| People minutes per correct transaction, modelled, ×1 | **2.11** | **2.31** |
| Same, ×0.5 / ×2 | 1.45 / 3.43 | 1.68 / 3.55 |
| Same, items only (no setup), ×1 | 1.32 | 1.25 |
| Model cost per item | $0.055 | $0.054 |
| Wall time per item, median / p90 | 26.9 s / 31.5 s | 59.4 s / 95.6 s |
| B's verification time per item, median | | 15.8 s |

- **Duplicates.** Both arms' duplicate is the same item: a fault case that submits the save twice. Both arms stopped on it.
- **Spend.** All model spend, calibration included, was $10.92, against the $300 cap.
- **Ordinary referrals.** Both arms got all 50 right, with no false stops.

### By class (counts only; the fault cases are private)

| Class | Cases | A | B |
|---|---:|---|---|
| wrong_value | 4 | 2 correct, 2 missed | 2 caught, 2 false stops |
| collateral | 2 | 2 missed | 2 caught |
| delayed_collateral | 3 (1 excluded) | 2 missed | 2 missed |
| side_record | 2 | 2 missed | 1 caught, 1 missed |
| concurrent_legitimate | 4 | 3 correct, 1 missed | 3 correct, 1 missed |
| mixed | 3 | 1 correct, 2 missed | 2 caught, 1 false stop |
| misleading_referral | 2 | 2 missed | 2 caught |
| duplicate | 2 | 1 caught, 1 correct stop | 1 caught, 1 correct stop |
| uncertain_write | 2 | 1 correct, 1 false stop | 2 correct |
| Perturbation: delayed legitimate write | 4 | 4 correct | 4 correct |
| Perturbation: restart after the save | 4 | 4 false stops | 4 correct |
| Perturbation: concurrent staff edit | 4 | 4 correct | 4 correct |
| Perturbation: timeout after a successful save | 4 | 4 correct | 4 correct |

What the counts show:

- **What B catches that A misses.** These are wrong values in the saved chart, writes outside the item, side records, and changes a referral asked for but should not have. A's REST read-back of the new chart does not see changes to other records. In several cases it also accepted a value that differed from the referral.
- **What B misses.**
  - Two are writes that land after B's check finishes: `delayed_collateral`, with `settle_s` at 2 s.
  - One is a delayed second side-record row.
  - One is a change made by another real user, which the audit log attributes to that user (`concurrent_legitimate`).
  
  A check that runs once at the end cannot see a write that lands after it.
- **B's false stops.**
  - Two come from wrong-value cases where the end state was acceptable to the oracle but did not match B's frozen contract.
  - One is a mixed case where a legitimate privilege change by another actor was not attributed to them.
- **A's false stops.** Four of the five come from restarts between the save and the read-back. A could not confirm its own save and stopped. B's whole-database check confirmed each of those saves.

### Timings under load

The host was shared with other sessions, and its 1-minute load went above 40 during the run (deviation 4). Correctness does not depend on load; timing does. These figures use the host load sampled every 30 s, taking the highest sample over each item's run. The earliest items (A 23, B 14) ran before sampling began and are left out of the second figure.

| Median wall time per item | A | B |
|---|---:|---:|
| All 90 items (raw) | 26.9 s | 59.4 s |
| Items whose highest sampled load was under 40 | 26.1 s (62 items) | 61.7 s (70 items) |

**The latency comparison rests on the raw figures.** The two agree to within 3 s, so load does not change the conclusion. B takes about 2.2 times as long per item; 15.8 s of that is the verification at the median.

### Pins

- **Engine.** The arms ran on integration commit `6425cf609884010be1650550e877f0070ea96324`, pushed as `bench/incremental-value-integration`.
- **What the commit contains.** `main` 0e88d7d98 merged with:
  - #1528 b5be96f5fcac;
  - #1588 722552b60920, which carries #1570 and #1582;
  - #1609 16e7d6cee464, which carries #1589.
  
  The record-check stack had not landed on `main` when the arms ran.
- **Scores.** The aggregate scores are in [`results/score.json`](results/score.json). The per-item ledgers name the fault cases, so they are private and kept alongside the fault set.

## The setup

The pre-registration is [`PREREGISTRATION.md`](PREREGISTRATION.md), committed in OpenAdaptAI/OpenAdapt#1626 before either arm ran. The blind fault brief is [`FAULT_BRIEF.md`](FAULT_BRIEF.md).

- **Fixture.** OpenEMR 8.0.0.3 (`openemr/openemr:8.0.0.3@sha256:0aa4d3d5…`) with MariaDB 11.8, from the pinned lock in `benchmark/openemr_local`. Data is synthetic.
- **Agent.** The same in both arms: #1528's `agent_tools` (`claude-sonnet-5-5`, effort medium, 40 turns, $0.50 per item). Its prompt and tools are unchanged.
- **A.** The agent checks its own work. It reads back through the REST patient search and checks for a duplicate before it saves.
- **B.** The same agent, plus OpenAdapt on the write.
  - `openadapt_flow.verifier.capture` runs before the agent acts, and `verify_write` runs after it finishes (#1588).
  - The checks are the record check over REST and the whole-database frame over MariaDB. The frame uses OpenEMR's app profile, the audit-log attribution and the `history_data` side record.
  - The #1609 hold applies: a second save request in an item is not sent.
  - The contract is [`contract.yaml`](contract.yaml) with [`audit_query.sql`](audit_query.sql). Their sha256 values, in [`CONTRACT.sha256`](CONTRACT.sha256), were committed before the fault cases were read.
- **Workload.** 90 items, in the same order for both arms (stream sha256 `c2f4db3a…`):
  - 50 ordinary referrals;
  - 24 blind fault cases;
  - 16 harness perturbations: delayed legitimate writes, a restart after the save, concurrent staff edits, and a timeout after a successful save.
- **Fault set.** `faultset6.yaml`, sha256 `61e245ad5bb2c5735135ee6bacff70c6e8aac617df1739a5ea44f18385c54629`. It is private, in openadapt-internal at `qualification/incremental-value/`. Its ops are there too. This README publishes only class counts and results by class.
- **Oracle.** [`oracle.py`](oracle.py) runs as the database root and diffs every table after each item. It imports nothing from `openadapt_flow`.
- **People time.** Modelled with cost model V1 (`economic_comparison/assumptions.json`). No operator was timed.

## Deviations from the pre-registration (dated 2026-10-28)

1. **Who wrote the fault ops.** The separate agent asked to write them stopped before writing anything, cut off by a safety filter on its output. So the harness author wrote them, after B's contract sha256 was committed (0372fdcd7). The contract was not changed after that. Each fallback from an API path to SQL is logged on its item.
2. **Calibration size.** The pre-registration says 3 calibration referrals; B's contract was calibrated on 15, all outside the stream. Each round of 3 found one false-stop cause on clean saves, and each was fixed:
   - the form's empty-string columns;
   - OAuth token bookkeeping written after the token response;
   - the dashboard's first-use settings;
   - OpenEMR logging a UI write to `patient_data` under the previous chart's id.
   The last round verified 3 of 3.
3. **Two fixture copies.** The arms ran in parallel on two copies of the same pinned fixture (A on ports 9582/9583, B on 9580/9581), each reset and seeded the same way, so both fit in one lease. Both copies were bootstrapped from the same lock, so only their generated secrets differ.
4. **Host load.** Other sessions on the shared host pushed the 1-minute load above the pre-registered limit of 40, once to 139. Items already running were finished.
   - At 22:30, after A's 33rd item and B's 22nd, both arms were stopped between items.
   - They were restarted with `--no-reset` (the fixture's state carried over) and a between-item load gate of 60, as the session coordinating the lease directed.
   - Load per item was recorded only after that restart. Earlier items are matched to the 30-second load samples instead, and items run before sampling began have no load figure.
   - Correctness outcomes do not depend on load.
5. **One fault case excluded from both arms.** Its op, written by the harness author (deviation 1), failed with a SQL error while preparing, before the agent ran. This happened in both arms. The case was not retried, because a retry would fail the same way: the fault is in the op, not the fixture. N is 89 in both arms.

## Reproduce

```bash
cd packages/flow/benchmark
python -m pytest incremental_value/test_offline.py
# with the fixture lease, INCREMENTAL_VALUE_FAULTSET and INCREMENTAL_VALUE_FAULT_OPS set:
python -m incremental_value.run --arm A --out DIR
python -m incremental_value.run --arm B --out DIR
python -m incremental_value.score DIR --json DIR/score.json
```
