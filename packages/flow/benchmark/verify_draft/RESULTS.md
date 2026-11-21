# Results: model-drafted write contracts, admitted by qualification

Protocol: [PROTOCOL.md](PROTOCOL.md) and its addendum. Runs were made on
2026-11-21 with `claude-opus-5-5` at effort `medium`. Data was synthetic,
browser-free and headless. Raw data for every run, void or not, is under
[results/](results/):

- `result.json`: the numbers;
- `draft/rounds/`: every draft and every admission case;
- `draft/contract.yaml`: the admitted contracts;
- `setup/`: the operator inputs;
- `spend.jsonl` and `spend-dev.jsonl`: every model call.

## Verdict

**The kill criterion fires on time.** No untuned app finished setup within
5 minutes:

- **Mealie, first valid run: 9.2 minutes, and no contract was admitted.**
- **Directus, reported run: 6.0 minutes**, admitted in round 2. The first
  draft passed every planted fault and every clean write. Round 1 was lost
  only to a rule stricter than the brief, which refuses an exclusion that
  never moved (below).

The other two clauses do not fire:

- **Person decisions:** 0 on every app. The command never needed its one
  question.
- **Missed planted faults:** no admitted contract missed one, in admission
  or held out. Across the three admitted contracts, the held-out check
  caught 24 of 24 applicable faults, with 0 false stops on 15 held-out
  clean writes.

**Qualification did its job.** It refused every unsound draft:

- a Mealie check that couldn't see a lost update;
- a Mealie check that stopped every clean write;
- every draft that didn't load.

Each refusal said why. No refused draft would have been safe to admit.

**Speed is now the constraint, not judgment.** One admission round takes
130 to 180 seconds on the lab apps. Each case reads a 28- or 62-table
database several times over an SSH tunnel. A model round takes 14 to 93
seconds. So two rounds already pass 5 minutes. Setup took 1.3 to 1.4
minutes on the two local apps, with one round each.

## Per-app table (reported runs)

| App | Tuned on? | Description | Setup minutes | Person decisions | Drafts admitted / rejected | Planted faults caught, admission | Planted faults caught, held out | False stops, admission clean writes | False stops, held-out clean writes | Model USD |
|---|---|---|---|---|---|---|---|---|---|---|
| MockLoan (create) | yes | `db` | 1.4 | 0 | 1 / 0 | 8/8 | 10/10 | 0/5 | 0/5 | 0.05 |
| Workbench, MockMed profile (update) | yes | `db` | 1.3 | 0 | 1 / 0 | 6/6 | 6/6 | 0/5 | 0/5 | 0.08 |
| Mealie v2.8.0 (update), first valid run | no | OpenAPI | 9.2 | 0 | 0 / 3 | best draft 6/8 | not run (nothing admitted) | best draft 0/5 | - | 0.58 |
| Directus 11.5.1 (update) | no | OpenAPI | 6.0 | 0 | 1 / 1 | 7/7 | 8/8 | 0/5 | 0/5 | 0.17 |

How to read the columns:

- "Planted faults caught" counts the applicable faults. One `wrong_value`
  case is planted per written column.
- Each admitted contract also confirmed every write in its attribution
  probe: 5 writes each (2 users x 2 payloads, plus the first write
  repeated).
- Model cost per contract is USD 0.05 to 0.17 for the admitted ones, and
  USD 0.58 for Mealie's refusal.

The benchmark spent USD 2.52 in all:

- USD 1.93 on the reported and void runs (`spend.jsonl`);
- USD 0.59 on development on the tuned apps (`spend-dev.jsonl`).

The cap was USD 30.

## What each case showed

**MockLoan.** The drafted check reads the records table. It matches all
four values and the surface, and declares the columns the app sets on a new
row. It refuted the faults planted on other rows with other values. It also
refuted the two app-level faults: the same disbursement sent to another
loan, and the disbursement sent twice. MockLoan has no sign-in, so its two
actors send identical requests. The lost-update case is not applicable to a
create.

**Workbench.** The drafted check reads the patient and counts the save's own
audit row. The audit row is tied to the patient by a join, and the frame
judges it through `judged_by`. The lost-update case ran, but it is not
applicable: the workbench refuses a save from a stale form (HTTP 409). The
workbench signs in one user, so its second actor is a second session of
that user, and the attribution probe has no real actor axis. There was no
app-level held-out fault, because the harness gave no second record to
write to.

**Mealie, the first untuned app.** Mealie keeps no version, author or save
history for a recipe. Its PATCH deletes and re-creates the recipe's child
rows (instructions, ingredients, nutrition, settings) on every save. So a
lost update leaves the store exactly as a correct save would. No draft
caught one. Two drafts tried to read the re-created child rows' ids as
versions. Those ids come from a shared sequence, not a per-recipe one, so
those drafts stopped every clean write. Admission refused all of them,
which is the right outcome for a store that can't show a lost update. It
also makes Mealie a poor test of the approach, which is why Directus was
added.

**Directus, the second untuned app.** The draft read four things:

- the ticket;
- the save's revision, joined to its activity row to get the author;
- `frame.versions` from the same join, with `author_field`, so the other
  user's save in the window refutes the agent's;
- the whole database, with the activity log append-only and the session
  table skipped.

All 17 cases passed in round 1, including the lost update. The draft was
rejected only because it also excluded `directus_users.last_access`. That
column moves at sign-in, not during a write, so it never moved in a probe.
Round 2 dropped that exclusion and was admitted. Held out, the contract
refuted all 8 applicable faults:

- the faults planted on another ticket with other values;
- the late revert, at the recheck;
- the lost update;
- the same write sent to another ticket through the app.

## Earlier runs (kept, not reported in the table)

| Run | What happened | Minutes | Model USD |
|---|---|---|---|
| Mealie run 1, void | Operator setup error: the read-only role had no `SELECT` on the sequences, so no baseline could be taken. Stopped after round 2. The command now checks the read account once before any model call. | - | 0.51 |
| Mealie run 3, after the fixes below | Refused after 3 rounds. The best draft caught 6 of 7 faults, missing only the lost update. | 4.0 | 0.44 |
| MockLoan and workbench, prompt 1 (`results/v1/`) | Admitted in round 1 with the same numbers as the reported runs. | 1.4 and 1.2 | 0.04 and 0.06 |

Mealie run 2 exposed three faults in the qualifier. They were fixed before
Directus, and the addendum lists them:

1. **Fresh row ids counted as written values.** Without a repeated write,
   the attribution probe counted the new id of a re-created row as moving
   with the payload. So it flagged a correct `skip` exclusion.
2. **A derived copy counted as a written column.** A lower-cased copy
   (`description_normalized`) was treated as a column the task writes, so
   a wrong value was planted there.
3. **Unloadable YAML.** The prompt asked for YAML, and unquoted colons in
   `reason:` strings made the YAML fail to load in two rounds. Prompt 2
   asks for JSON.

Mealie run 2 also counted rounds in a way the harness has since fixed: its
`result.json` says 2 drafts, and there were 3.

## Limits of this evidence

- **Held-out coverage.** The held-out check ran only on admitted contracts.
  That means the two tuned apps and Directus, not Mealie.
- **The faults are planted by OpenAdapt.** Most are written to the store
  with the privileged connection. Only the lost update and the two
  app-level held-out faults go through the app. No grader written by
  someone else judged these runs; the blind trial covers that separately.
- **The attribution probe's definition is this PR's.** The brief describes
  it as "two actors by two payloads"; [VERIFY_DRAFT.md](../../docs/VERIFY_DRAFT.md)
  defines it. The rule that refuses an exclusion that never moved is
  stricter than the brief, and it cost Directus a round. A rule that also
  accepts an exclusion of a column the app moves at sign-in would need a
  sign-in in the probe. That's a possible next step, not measured here.
- **Setup minutes are command wall time.** They don't include the
  operator's work: starting the app, creating users, recording the trace
  and writing `sandbox.yaml`. The harness scripted that work in 1 to 5
  seconds (`operator_setup_seconds`). A person doing it by hand would take
  longer.
- **Host load.** The Mac's 1-minute load was 36 at the start of the
  Directus run and 18 at the end (`results/directus.load`). Most of the time went to database reads
  on the lab host and to model calls.
- **One task per app.** One run per app, so no spread is reported.
