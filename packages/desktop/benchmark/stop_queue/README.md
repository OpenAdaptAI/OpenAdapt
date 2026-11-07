# Seconds per stop: the stop queue on 20 recorded soak stops

How long does a person take to decide one stop, before and after the stop
queue (Desktop's **Review stops**)?

## The stops

The 20 stops are the first 20 that the scripted operator handled in the
`after` arm of the production soak of 2026-10-24. That soak ran the
OpenEMR referral fixture with synthetic patients, and the files are in
`packages/flow/benchmark/lifecycle/results/production-2026-10-24/after/`.
The stops break down as follows:

- 15 "Same person?" stops, where the duplicate check found a possible match:
  - 8 matches are a different person, so the right answer is to add the patient;
  - 7 are the same patient (a resend, a chart that already exists, or a chart
    another clerk made a moment earlier), so the right answer is not to add it.
- 3 "Did it save?" stops, where the record check couldn't confirm the save:
  - 2 saves went in wrong;
  - 1 save is right.
- 2 stops on the item's inputs: one has no date of birth, and one is missing
  an optional phone number.

`build_drill.py` turns each stop into the card Desktop's `stop_queue` builds
for it, so the drill uses the same code as the app. The "possible match" rows
are reconstructed from the soak, because the soak did not keep the app's list.
A chart is listed when it existed at that moment and shares the item's last
name or date of birth, which is the rule the duplicate check applies. The
charts that existed are:

- the starting charts;
- charts that earlier items made;
- a chart another clerk made just before the item.

The ground truth gives the right answer for each card. A card is **hard**
when a quick glance would get it wrong: the possible match is a different
person, or the save went in wrong. Ten of the 20 are hard.

```bash
cd packages/desktop
uv run python benchmark/stop_queue/build_drill.py \
  --arm ../flow/benchmark/lifecycle/results/production-2026-10-24/after \
  --out benchmark/stop_queue/drill-2026-10-24.json
```

## Estimate: keystroke-level model

`klm.py` applies the keystroke-level model to the steps each stop takes. It
uses the operator times from Card, Moran and Newell (1983), as given by
Kieras (2001):

| Operator | Seconds |
| --- | --- |
| Keystroke | 0.28 |
| Point | 1.1 |
| Click | 0.2 |
| Move hand between keyboard and mouse | 0.4 |
| Mental act (read, compare one value, decide) | 1.2 |

It also assumes 1.5 s for the app's patient search and 1.0 s for a Touch ID
prompt. It counts deciding only, not entering an item by hand afterwards.
The steps in each arm are:

- **Before** (Needs you today):
  1. Open the item.
  2. Read why it stopped.
  3. Remember the key values.
  4. Switch to the app, search the last name, and open and compare each
     listed record.
  5. Switch back, tick "I looked at the record", and pick the action.
- **After** (the card):
  1. Read the question.
  2. Scan the marked comparison for each possible match.
  3. Decide, press one key, and confirm Touch ID.

The results are in `klm-2026-10-24.json`:

| Stops | Before, mean | After, mean | Before, median | After, median |
| --- | --- | --- | --- | --- |
| All 20 | 26.7 s | 8.2 s | 27.7 s | 5.3 s |
| 15 "Same person?" | 29.6 s | 5.5 s | 27.9 s | 5.3 s |
| 3 "Did it save?" | 25.6 s | 23.5 s | 25.5 s | 23.4 s |

**This is a model, not a measurement of a person.** The model is what puts
"Same person?" cards in seconds: the card shows the item next to each
possible match, field by field, and marks each value Same or Different, so
no app lookup is needed.

"Did it save?" cards barely change. Their cards don't carry the values the
record check read back, so the person still looks the record up in the app.
The next step is to add those read-back values to the halt.

For scale, the soak's cost model charged 90 s for a stop plus a 30 s
interrupt; it is an assumption too.

## Measuring a person

The practice page runs the real **Review stops** screen on the 20 cards.
Nothing is signed or saved. It records each answer and how long its card was
on screen.

```bash
cd packages/desktop
npx vite --port 1420
# open http://localhost:1420/benchmark/stop_queue/practice.html
```

The score is accuracy only. It says how many stops were right and how many
hard ones the person caught. Time goes into the downloaded results for this
measurement; it is never scored or ranked. Answering blindly scores
accordingly: pressing 1 on every card gets 8 of 20 and catches none of the 10
hard stops.

For "before", time the same 20 stops through Needs you and the app, item by
item. In the product, every answer from **Review stops** is also logged with
its time on screen (`stop_decisions.jsonl`, `shown_ms`). Real operator times
can therefore replace the cost model's 90 s assumption.

No person has run either arm yet.
