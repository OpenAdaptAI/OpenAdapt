# Pilot measurement protocol

Internal. This is how we measure a pilot at a customer site, and how the lifecycle
soak (`benchmark/lifecycle`) records the same numbers on our own fixture. It is not
customer-facing copy.

## The question

Does OpenAdapt remove work for the people who own the workflow? Stopping safely is
necessary but not enough. A pilot succeeds when the operators spend fewer minutes
per verified item than they spend doing the work by hand, over weeks, including
every pause, approval, reconciliation and repair.

## The headline number

**Verified completed items per operator-minute**, on one production configuration,
over the whole pilot.

- **Verified completed item.** An item whose final state an independent check
  confirms, and that no person had to enter or fix by hand. The check is the
  customer's own system of record (a read-only query, a report, or a sampled audit),
  never OpenAdapt's own verdict.
- **Operator minutes.** Every minute a person spends because of the automation:
  - approvals the approval mode asks for;
  - stops: reading the reason, looking at the record, deciding;
  - reconciliations: a save may have landed, and a person checks;
  - resumes after a durable pause;
  - repair reviews and re-admissions after an app change;
  - items the person finishes or redoes by hand.
- **The baseline.** The same people's minutes per item by hand, measured on the same
  item types before the pilot starts (see below).

Report it at two scopes: run only, and including setup (teach, compile, qualify,
admit). Report beside it, never folded in:

| Number | Definition |
| --- | --- |
| False stops per 100 items | Stops on items whose data was right and where nothing was wrong in the app. |
| Operator minutes per stop | All minutes charged to stopped items, divided by the number of stopped items. |
| Silent wrongs | Items OpenAdapt called done that the independent check found wrong. The target is zero. Every one is investigated. |
| Maintenance minutes per app change | Minutes from the first stop an app release caused until the program verifies items again: stops, repair review, re-qualification attended time, re-admission. |
| Time to running again | Wall time from an app change to the next verified item. Most of it is machine time (re-qualification); report it separately from operator minutes. |
| Onboarding hours | Teach, compile review, qualification review and admission, per workflow. |

## Before the pilot

1. **Pick one configuration and freeze it.** Write down the profile, the approval
   mode, durability, read-back, the repair route and the pinned bundle digest. A
   configuration change during the pilot starts a new measurement period.
2. **Measure the manual baseline.** For 20 to 30 items of each item type, record
   how long the operator takes by hand: from opening the source document to the
   saved record, including any lookup (duplicate search, eligibility). Use screen
   time from Desktop's recorder or a stopwatch log, not estimates. Keep the
   per-item times, not only the mean.
3. **Agree on the independent check.** Name the query or report that decides
   whether an item is right, who runs it, and how often. For a sampled audit,
   agree the sample size up front.
4. **Agree on what counts as an exception.** Duplicates, missing data and items
   the business returns to the sender are right stops, not false stops. List the
   exception types with the customer before day one.

## During the pilot

Record every operator touch with a timestamp at both ends: when the request
reached the person and when they answered. Desktop and the decision portal record
the request and answer times for approvals and stops. For the rest (a
reconciliation done in the app, a hand entry), the operator logs start and end in
the exception inbox.

Use measured times, not the cost model, wherever a timestamp exists. The time a
request waits in the queue is not operator time; the time the person works on it
is. When only the answer time exists, charge the cost model's per-touch number and
mark the row as estimated.

Weekly, record:

- items received, items verified, items stopped (by reason), items done by hand;
- operator minutes, by touch kind;
- app releases or configuration changes, with the date;
- the independent check's result for the week.

## After an app change

App releases are where maintenance shows up. For each release that stops items:

1. Note the date and what changed (release notes, or what the stop showed).
2. Count the items that stopped before the program ran again.
3. Record each recovery step and who did it:
   - the stop review;
   - the repair proposal and its review (for a relabeled control, the relabel
     repair proves only the label changed; see `openadapt_flow/repair/relabel.py`);
   - re-qualification on the test copy (machine time);
   - the re-admission (a person's approval; under a production qualification
     admission, the qualification authority's signature);
   - the first verified item afterwards.
4. Charge the operator minutes and the wall time separately.

Under a qualified or managed run, self-repair stays refused by design
(`docs/COMPATIBILITY.md`). The path back is re-qualification plus a fresh admission
from the authority. Measure how long that takes end to end; that wait is a real
cost of the configuration.

## Over weeks

The pilot lasts long enough to see at least one app release, and preferably four
weeks or more. Plot, per week:

- verified items per operator-minute (run only);
- false stops per 100 items;
- maintenance minutes.

A configuration that removes work shows a headline above the manual baseline that
holds after the first release, with maintenance minutes small next to the minutes
saved.

## How the soak records the same numbers

`benchmark/lifecycle` is a synthetic customer on the local OpenEMR fixture.

- `workload.py` writes the item stream and its ground truth (exception types and
  the right outcome of each) before the run.
- `apps.py` applies every event: other clerks, forced logouts, slow and failed
  saves, and app changes (`--drift KIND@SEQ`, for example `relabel_save@26`).
- `oracle.py` judges every item by a direct SQL read of the database, never by
  the product's verdict.
- `operator.py` answers every request from the ground truth and a look at the app.
  Each answer is charged from `cost_model.py`, which is versioned. No real operator
  timings exist yet, so every charge is an assumption, and the report shows x0.5
  and x2 bands.
- `metrics.py` computes the headline (`headline` in `results.json`) and its parts
  from `ledger.jsonl` only. `python -m benchmark.lifecycle.report <run dir>`
  recomputes them.
- `production_arm.py` runs the production configuration (Regulated,
  `problems_only`, durable, pipelined read-back, repair on) per item, with
  `recovery: "relabel"` for the relabel repair path.

At a customer, replace the oracle with the agreed independent check, the scripted
operator with the real operators and their measured times, and the seeded stream
with the real queue. Keep the ledger format, so the same `metrics.py` computes the
same numbers.
