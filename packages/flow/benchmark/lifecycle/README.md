# Lifecycle soak: does OpenAdapt remove work under ordinary business conditions?

A "synthetic customer": a seeded stream of referrals is worked through on a
real local OpenEMR 8.0.0.3, with duplicates, bad source documents, other
clerks, forced logouts, slow and failed saves and four UI changes, while a
scripted operator answers every stop. The result is one number with its
parts, computed only from the run's ledger.

**Verified completed items per operator-hour**: items whose final state the
independent oracle judges right and that the operator did not do by hand,
divided by every operator hour the run charged (setup, per-item requests,
maintenance after UI changes).

Reported beside it, never folded in:

- setup minutes (teach, compile, qualify, admit);
- per-item operator minutes (approvals, stops, reconciliations, repair reviews);
- maintenance minutes after each UI change;
- false-stop rate: stops with nothing written on items whose data was right
  and that carried no injected fault, over all such items (Wilson 95% CI), and
  how many of them happened with a UI change in force;
- silent-wrong rate: items the arm claimed done that the oracle judged wrong,
  over all items it claimed done; the oracle is a direct SQL read of
  `patient_data` from inside the database container, never the product's
  verdict;
- wall time, host load, model calls and list-price cost;
- the same headline if every silent wrong is later found and fixed
  (reconcile + re-entry per silent wrong), and the all-manual baseline
  (every item entered by hand).

## Files

| File | What it is |
|---|---|
| `arm.py` | The interface every arm implements (`lifecycle-arm/1`) |
| `workload.py` | The seeded referral stream, its exception mix and ground truth |
| `apps.py` | The OpenEMR adapter: reset, SQL oracle channel, every injected event |
| `oracle.py` | The independent judge (per item, and the item's final state) |
| `operator.py` | The scripted operator and its decision rules |
| `cost_model.py` | Seconds charged per operator touch, versioned, with its calibration note |
| `metrics.py` | The metric and its parts, from the ledger |
| `report.py` | `results.json`, `REPORT.md` and `stops.jsonl` from the ledger |
| `run.py` | The runner |
| `openadapt_arm.py` | The OpenAdapt arm |
| `fake.py` | An in-memory app and two toy arms, for tests and for checking a new arm |
| `results/<run>/` | Committed runs: `ledger.jsonl`, `workload.jsonl`, `results.json`, `REPORT.md`, `stops.jsonl` |

## Run it

```bash
cd packages/flow/benchmark
python -m lifecycle.apps up                    # the lifecycle OpenEMR copy, ports 9420/9421
python -m lifecycle.run --app openemr --seed 7 --items 300 --days 10 \
    --arm lifecycle.openadapt_arm:make --out lifecycle/results/<run id>
python -m lifecycle.report lifecycle/results/<run id>   # recompute from the ledger
```

`--app fake --arm lifecycle.fake:careful` runs the whole pipeline in a second
with no fixture. Two runs with the same seed see the same items, events and
UI changes, in the same order.

## The workload

Each item is one new-patient referral (the 12 fields of OpenEMR's New
Patient form). About a quarter carry an exception (`workload.py` has the
exact mix and the right outcome of each): duplicates of existing charts,
resent referrals, same-name patients who are and are not the same person, a
missing date of birth, a missing home phone, a US-format date of birth, names
in capitals with stray spaces, another clerk registering the same patient
first, another clerk editing an unrelated chart mid-item, a forced logout, a
slow save and a save that fails once. The mix is stress-weighted to exercise
every kind several times, not a measured intake distribution.

UI changes are applied in the app server, so every client sees them, and
stay on to the end of the run: a theme (day 2), a relabel of both create
buttons (day 4), a reordered demographics layout (day 6) and a notice that
must be dismissed before the form takes input (day 8). A version bump is not
run: the fixture pins one OpenEMR image.

Every injected event, with exactly what was changed, is a row in the ledger.

## The operator

`operator.py` answers every request from the ledger's truth and from a
look at the app, never from the arm's verdict, and records the rule that
decided. Requests and their charges come from `cost_model.py`. No real
operator timings exist yet, so every charge is an assumption, stated in the
model and shown at x0.5 and x2.

## Adding an arm

Implement `arm.Arm` and pass `--arm module:factory`. Route every human touch
through `operator.ask`; never charge yourself and never judge yourself.
`Env.extra` carries the fixture's ports and read-only and actor REST
sessions; arms never get the oracle's SQL. Raise `arm.SpendCapReached` to
pause an arm at a spend cap.

## Limits

- One machine, shared with other work; every item row records the 1-minute
  load. Wall times are not comparable across runs at different loads.
- Synthetic data, a synthetic workload mix, and assumed operator costs.
- The OpenAdapt arm's configuration, and what the product refused, is in
  each run's `results.json` (`arms.<arm>.describe`) and its report.
