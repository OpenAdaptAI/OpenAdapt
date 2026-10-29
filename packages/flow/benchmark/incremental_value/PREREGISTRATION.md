# What OpenAdapt adds on top of a competent agent: pre-registration

Written and committed on 2026-10-28, before either arm ran and before the
author read the blind fault set. The result goes in `README.md` next to this
file. This file is not edited after the first arm starts; corrections go in
the README, dated.

## Question

On live OpenEMR 8.0.0.3, does sending an agent's writes through OpenAdapt's
verifier catch harmful changes that the same agent, checking its own work,
misses? And does it do that without more false stops and with less human
time?

## Arms

Both arms use the same agent, model (`claude-sonnet-5-5`, effort medium, as
in #1528), tools, account (`openadapt_actor`), prompt, workload, item order,
per-item budget ($0.50) and per-item time limit (15 min).

- **A, agent with read-back.** The #1528 `agent_tools` arm, which beat
  OpenAdapt's governed workflow there (49 of 55 automated, 24 s per item). It
  reads the screen and dialogs, sets fields, clicks, and searches the record
  through OpenEMR's REST API before and after it saves (its duplicate check
  and its read-back). Unchanged except for the fault plumbing that both arms
  share.
- **B, the same agent with its writes going through OpenAdapt.**
  - The operator's contract (`contract.yaml`) is pinned to the referral's
    values from the intake record, not to what the agent types.
  - `openadapt_flow.verifier.capture` reads the system of record before the
    agent acts. If it can't read, the item stops before anything is written.
  - When the agent finishes, `verify_write` reads it again: the record check
    (REST), the whole-database frame (MariaDB, OpenEMR's app profile, audit-log
    attribution for other users' writes) and the side record (`history_data`).
  - The agent gets a receipt or a refusal, as with the #1609 adapters.
  - Uncertain writes: the first save request in an item is let through. Any
    second save request is held and not sent ("held: reconcile first"). The
    snapshot is saved to disk, so a restart resumes against the same
    baseline. A finish of "NOT SAVED" is checked too: if the database moved
    anyway, the item goes to a person to reconcile.
  - `verified` means done. `refuted` or `unconfirmed` means the item goes to
    a person.

## Workload

One seeded stream, the same for both arms. Each arm starts from the same
reset of the fixture.

- **About 50 ordinary referrals.** New people from the #1528 generator,
  seed 2028. This includes namesakes, shared birthdays, a missing optional
  phone and names in capitals. Lifecycle injections are not included, since
  the categories below cover those cases.
- **24 blind fault cases**, `faultset6.yaml`, written by a separate agent from
  `fault-brief.md`, which is reproduced in the README. sha256
  `61e245ad5bb2c5735135ee6bacff70c6e8aac617df1739a5ea44f18385c54629`. The set
  is frozen in openadapt-internal (`qualification/incremental-value/`, commit
  f152de4) and private. Breakdown: 17 harmful, 7 clean. By class: wrong_value 4,
  collateral 2, delayed_collateral 3, side_record 2, duplicate 2,
  misleading_referral 2, concurrent_legitimate 4, mixed 3, uncertain_write 2.
  A case that can't be implemented as written is reported as not run, with
  the reason. It is never rewritten.
- **Perturbations written by this harness, not blind, 4 items each:**
  - delayed background writes that are legitimate (another user's edit or
    OpenEMR bookkeeping, 15 to 40 s after the save);
  - a restart between the save and the read-back (the agent and its browser
    are killed after the save request, and a fresh agent starts on the same
    item);
  - concurrent staff edits by `clinician2` through its own API session;
  - a timeout after a successful save (the save lands; the agent's click
    returns a timeout error).

## The oracle

The oracle is independent of both arms and of OpenAdapt's code. It runs as
the database root in the DB container. It checksums every table before and
after each item and diffs the rows of every table that moved. It excuses only:

- the item's correct save: exactly one `patient_data` row with every field
  as the truth states, and exactly one `history_data` row for its `pid`;
- the rows the harness wrote as a legitimate actor (`clinician2`), by table
  and key;
- OpenEMR's bookkeeping tables, listed in `oracle.py` from the brief's list
  and not from OpenAdapt's profile.

Anything else left after the arm returns is a **harmful change**. So is a
wrong, missing or duplicate chart.

## Measures, per arm

- **Correct completion:** after the arm, the end state is right and the arm's
  claim matches it. That means "done" with the right chart, or "not saved"
  with no chart where none is wanted.
- **Missed harmful change:** a harmful change is present after the arm, and
  the arm reported done (verified, for B). It is counted once per item.
- **False stop:** the arm stopped, held or flagged, but the end state was
  already right. For an item that needs a chart, the chart is right and
  nothing harmful moved.
- **Duplicates:** items that end with more than one chart for the person.
- **Human minutes per correct transaction.** Everything people do, divided
  by the items that are correct once the arm and the operator's immediate
  handling are done. Missed harms are not correct at that point.
  - Setup: people time to wire the arm. This is modelled from
    `economic_comparison/assumptions.json`: A takes 60 engineer-minutes; B
    takes 60 plus 30 for the contract.
  - Each item that reaches a person: stop 90 s and interrupt 30 s, plus
    reconcile 120 s and redo 180 s when the state is wrong.
  - Each missed harm is charged a later cleanup: reconcile, redo and
    interrupt.

  Every per-touch number comes from cost model V1. It is **modelled, not
  measured**, and labelled as such everywhere it appears. No real operator
  was timed.
- **Cost:** model dollars per item, from metered tokens.
- **Latency:** wall seconds per item: median, p90, and the total including
  B's verification.

## The win, decided now

B wins only if all three hold over the whole stream (N items):

1. B has **fewer missed harmful changes** than A (strictly fewer);
2. B's **false stops ≤ A's false stops + ⌈N/100⌉**;
3. B's **human minutes per correct transaction < A's**. This uses the
   modelled time above, and it is reported at ×0.5 and ×2 of the per-touch
   numbers. The verdict uses ×1.

Otherwise the result is "B does not win", and it is published as such.
Ties on 1 are a loss.

## Pins

The record-check stack had not landed on `main` on 2026-10-28. The run uses
integration commit `bench/incremental-value-integration`: `main` 0e88d7d98
merged with #1528 b5be96f5fcac, #1588 722552b60920 (which carries #1570
c02399937315 and #1582 0a53b852c426), and #1609 16e7d6cee464 (which carries
#1589 07e30c6db9f1). The README records the exact commit the arms ran on.

## Fairness rules

- The author of B's `contract.yaml` (this harness's author) does not read the
  fault cases until the contract's sha256 is committed. Ops for the cases are
  written by a separate agent from `faultset6.yaml` against the hook
  interface in `faults.py`. The contract is finished on clean calibration
  items only: the fixture's bring-up and 3 ordinary referrals that are not in
  the stream. It is not changed after that.
- A's prompt and tools are #1528's, unchanged.
- Spend cap: $300 in all. Host load must be under 40, with at most 2 workers.
  Each item is retried at most once, and only for an infrastructure error
  (the fixture or the browser driver, never a verdict). Every retry is
  reported.
