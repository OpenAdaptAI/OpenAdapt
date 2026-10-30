# Pre-registration, run 3: what OpenAdapt adds on Medplum, with the setup and review levers

**This is a new test on a different app.** It neither replaces nor
reinterprets runs 1 and 2 ([`PREREGISTRATION.md`](PREREGISTRATION.md) and
[`PREREGISTRATION_2.md`](PREREGISTRATION_2.md)), which ran on OpenEMR and
stand as published.

Run 1 lost on people time: about 30 minutes of contract setup, and 420
modelled seconds per caught harm. Run 3 measures the two levers built for
that:

- contracts generated from demonstrations (#1653);
- a one-tap stop card (#1653, `stop_card/PREREG_REVIEW.md`).

It also measures the verifier changes that make them usable:

- `void` (#1657);
- the agent's own corrections (#1655).

All four run only on `openadapt_flow.ensure_record`, which checks a write
through FHIR and its audit trail. OpenEMR's verifier is a different one, a
whole-database frame. So run 3 runs on **Medplum 5.2.8**, the FHIR server
that `ensure_record` was qualified on (#1640).

This file is committed before any run-3 item runs. Nothing in it changes
after the first item starts. Anything that has to change is added later as a
dated deviation.

## Order of events, all before this file

1. **2026-10-30 01:26 UTC.** A separate agent wrote the blind fault set from
   [`FAULT_BRIEF_3.md`](FAULT_BRIEF_3.md), published here and kept beside the
   set in openadapt-internal. It was frozen by sha256 at commit 14a055c. The
   agent read nothing else of OpenAdapt's.
2. **02:12–02:14 UTC.** B's three contracts were generated and reviewed by a
   person who hadn't read the fault set
   ([`PERSON_LOG_3.md`](../fhir_medplum/run3/PERSON_LOG_3.md)). They were
   frozen by sha256 in openadapt-internal `CONTRACT_FREEZE.md` at commit
   07b37e0.
3. Only then was the fault set read on the B side. Three things were written
   from it:
   - the harness's handling of its four `describe:` cases, as existing ops in
     a private file, frozen by sha256;
   - the resolution of the brief's default patients, by person;
   - the detection of two identical patients.
4. An independent agent reviewed arm A for fairness before the freeze
   ([`REVIEW_ARM_A.md`](../fhir_medplum/run3/REVIEW_ARM_A.md)). Every finding
   it raised was fixed or is stated here.
5. A smoke run of 3 stream items checked the stack end to end
   ([Smoke run](#smoke-run)). Its results are discarded, and those items
   run again in the stream.

## Arms

The design is **paired**: one agent run per item, which both arms judge.

- **The agent** is Claude through the Claude Agent SDK, `claude-sonnet-5-5`,
  with effort medium, 40 turns and a $0.50 budget per item. These are run
  2's model, effort, turns and budget.
  - It has one tool, `fhir_request` (method, path, body), plus `finish`. No
    built-in tools are on, and tool output is capped at 20,000 characters,
    as in run 2.
  - Its system prompt
    ([`run3/run.py`](../fhir_medplum/run3/run.py), `SYSTEM`) asks for run 2's
    arm-A checks, in FHIR terms:
    - read the record back by id and compare every value;
    - search again for exactly one match;
    - fix a wrong value once;
    - search before retrying a failed write, and never leave two records;
    - report `not_done` if it can't confirm.
  - If it stops without calling `finish`, it is resumed once and asked to,
    as run 2's loop did.
  - The tool sends no headers, so the agent has no conditional create. That
    keeps it at parity with run 2's agent, which had no equivalent.
- **A** is the agent's own claim: done or not done.
- **B** is the verifier's outcome on the same run, whatever the agent claimed,
  as in run 2. That is `ensure_record` with B's frozen generated contracts:
  - `created_verified` or `found_existing` is done;
  - `needs_review`, including any unreadable read, is a stop.

  B's patient and practitioner references come from a search by the name and
  date of birth the agent is given, made before the agent starts. They never
  come from the harness's ids. If 0 or 2+ records match, that is B's stop.
- **B's stop card.** Every B stop gets a card, built and acted on after the
  oracle's last read, with a timed Undo or Fix and a read-back
  (`contract_autogen/blind.py stop_card`).

**Why paired:** B is passive on Medplum. It reads after the agent finishes
and doesn't hold or change the agent's writes, the way run 2's in-run hold
did. So the agent's behaviour is the same under both arms, and pairing
removes the agent's run-to-run variance from the comparison.

## Workload

**N = 300 items**, in one order, with seed 2031
([`run3/stream.py`](../fhir_medplum/run3/stream.py)). This is run 2's mix:

- **168 ordinary tasks,** a new person or slot each, with no fault. They
  rotate through register a patient, record an allergy, and book an
  appointment.
- **80 blind fault cases.** The fault set is `faultset_medplum_1.yaml`.
  - sha256 is
    `124a53d78a6c4c1e7141a7f357db2b5240356811040122e20859d5b7f58d1993`.
  - It is private, in openadapt-internal at
    `qualification/incremental-value-3/`. Only its hash, its counts and its
    results by class are published.
  - Counts by expectation: 52 harmful, 22 clean, 6 ambiguous.
  - Counts by type: 25 patient, 28 allergy, 27 appointment.
  - Counts by class: wrong_value 10, wrong_target 6, duplicate 5,
    lost_response 5, fake_success 5, refused_write 3, collateral 7,
    late_collateral 6, concurrent_legitimate 8, mixed 6, misleading_task 5,
    lookalike_clean 8, ambiguous 6.
  - Its four `describe:` cases run from `described_ops.yaml` (sha256
    `2e607f7463ac8f800358c80f936278c05c04b5c4ecc59b176c65ce2c63e64d31`, same
    folder), as written. No case is expected to be "not run".
  - The brief named the default patients by person, so a blind task's
    patient is the seeded record of that person.
- **52 harness perturbations,** 13 of each kind. Each leaves a correct end
  state when the agent works right:
  - the create lands and the agent gets a 504;
  - another clinician writes another patient's record as the agent writes;
  - another clinician writes after the agent finishes and before the check;
  - the agent's connection is cut after its first write lands, and a fresh
    agent runs the same task. The restart shares the item's one budget, as
    in run 2.

The oracle decides every item from the server's end state with its own
rules ([`public/oracle.py`](../fhir_medplum/public/oracle.py)), never the
verifier's. The fault author's `expect` doesn't decide anything. It is
compared with the oracle's verdict, and the comparison is reported.

## Schedule

- 6 chunks of 50 items, in item order (`--items 1-50`, `51-100`, and so on),
  each under the fixture lease from `~/oa/scratch/fixture-lease/QUEUE`. The
  lease is released between chunks.
- Each chunk appends to one `rows.jsonl`, so a chunk that stops resumes where
  it stopped.
- The project is emptied before every item, and Medplum is not reset
  between chunks.

## Outcomes and exclusions

The outcomes are the fair test's (`public/score.py`, from run 2's scorer):
correct, false stop, caught harm, missed harm, correct stop, failed stop,
silent absence.

An item is **left out of both arms**, and reported with its cause, in three
cases:

- the harness or the agent's infrastructure failed (an SDK error other than
  the agent running out of turns or budget). The item isn't retried, because
  a retry wouldn't start from the same state;
- a fault-set op couldn't be implemented;
- the fault never did what it describes. For example, the agent never made
  the write it applies to.

B's unreadable reads are B's stops, not exclusions.

## Measures and costs

The measures are run 2's:

- the outcomes above, and duplicates;
- people minutes per correct transaction;
- model cost per item;
- median and p90 wall time per item.

People time is costed from [`costs_3.json`](costs_3.json), frozen with this
file. It is `costs_2.json`, unchanged, except in two places.

- **B's stops are charged from the stop card. These seconds are modelled,
  not timed with people.**
  - A caught harm costs the card's modelled person seconds at x1:
    published KLM operators, reading at 238 wpm, the timed machine step, and
    the 300 s fallback when no one-tap action applies (`PREREG_REVIEW.md`).
  - Any other B stop costs the larger of the card's seconds and
    `costs_2.json`'s charge for that outcome. A card that would undo a
    correct record is charged, never credited.
  - **The V1 charge, 420 s per caught harm for B too, is reported as a
    sensitivity**, with the verdict at that charge.
  - A's stops are charged as in run 2: 420 s for a caught harm, 120 s for a
    false or correct stop.
- **Setup.**
  - A: 60 engineer-minutes, as run 2's A.
  - B: the same 60 minutes for the agent, plus the **measured 79 s** of
    person time on the three contracts (`PERSON_LOG_3.md`), plus **Medplum's
    one-time read-path qualification, about 35 minutes**
    (`fhir_medplum/readpath/MINUTES.md`).
  - The qualification is charged in the primary figure. It's done once per
    server version, not once per customer, but this run can't show it is
    already paid for any given customer.
  - The result is reported both ways. A secondary figure amortizes setup
    over 1,000 items, as in run 2.
- The missed-harm charge is run 2's cited 14,737 s, with the same low (330 s)
  and high (153,518 s) sensitivities. Every figure is also reported at x0.5
  and x2. The verdict uses x1.

## The win, decided now

These are run 1's and run 2's three criteria. B wins only if all three hold
over the N scored items:

1. B has **strictly fewer missed harmful changes** than A. A tie is a loss.
2. B's **false stops ≤ A's false stops + ⌈N/100⌉.**
3. B's **people minutes per correct transaction < A's**, at x1, with
   `costs_3.json`, the card's modelled seconds, and setup including the
   read-path qualification.

Otherwise the result is "B does not win", and it is published as such. The
verdict at 420 s per caught harm and the verdict without the qualification
are published beside it, but they don't replace it.

## Pins and run rules

- **Engine:** the `main` commit that merges this file's PR, with #1653,
  #1655 and #1657 on `main`. `chunks.jsonl` records it per chunk as `commit`.
- **B's contracts:** `fhir_medplum/run3/contracts/`, at the sha256 values in
  `CONTRACT_FREEZE.md`:
  - `patient.yaml`:
    `b7bf94edc09e7594ab0e328e72b3e432398823441d31a14360e9f453763bec7c`
  - `allergy.yaml`:
    `b78b67e954cb4088b09895bda45a3ff6c56c442d4d0c967254ea7ec13affe77f`
  - `appointment.yaml`:
    `b51ccc2a7df5d7218175600b8662ee1abfae827268fcf29c124322f22a4a2828`

  `chunks.jsonl` records them per chunk, and a mismatch stops the run.
- **Fixture:** Medplum 5.2.8 and its pinned images
  (`fhir_medplum/environment.lock.json`), on loopback, under Podman.
- **Running it:**
  `python -m benchmark.fhir_medplum.run3.run --faultset <frozen> --sha256 124a53d7… --described <frozen> --contracts benchmark/fhir_medplum/run3/contracts --out <dir> --items <chunk>`.
  It refuses a fault set with a different digest.
- **Scoring:** `python -m benchmark.fhir_medplum.run3.score <dir>/rows.jsonl`.
- **Host load:** each item records the load at its start and end. A chunk
  doesn't start while the 1-minute load is 60 or more. Timings are reported
  raw and for items run under load 40.
- **Model spend:** under $150 in all.
- **Results:**
  - per-item rows stay in openadapt-internal;
  - the scorecard, the counts by class and source, and the author-versus-oracle
    comparison are published.

## Known limitations, stated now

- **One false-stop mode B can't avoid.** In the smoke run, the agent wrote
  `telecom.use: home` for the task's "home phone". B's generated Patient
  contract is closed, and the demonstrations had no `use`, so B stopped a
  correct record. The contracts aren't changed: that item is in the stream,
  and changing them would calibrate on it. This mode will cost B false stops
  wherever the agent does it.
- **B has no late recheck on Medplum.** A write that lands after B's check is
  seen by the oracle, not by B. The fault set's late cases (`late_collateral`
  and the three late `describe:` cases) count against B wherever they're
  harmful.
- **The card's seconds are modelled.** No person was timed on the card. If
  real review is slower, the 420 s sensitivity shows what happens.
- **One agent, one model.** A different agent may check its own work more or
  less well than this one.

## Smoke run

On 2026-10-30, before this file, on the same stack, 3 stream items were run
to check the agent, the SDK options, the fault proxy, the restart and the
card: items 12 (ordinary patient), 20 (restart, allergy) and 21 (lost
response, appointment).

All three ran end to end. The SDK accepted effort and budget, and every
agent run returned `success`. The oracle found all three correct. A said
done on all three. B said `created_verified` on 20 and 21, and stopped 12
(the false stop above, with a Fix card). The rows are kept in
openadapt-internal `qualification/incremental-value-3/smoke/` and discarded
from the result. The three items run again in the stream like any other.
