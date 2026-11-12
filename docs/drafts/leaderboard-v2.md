<!-- DRAFT. Not published. The founder approves all public text before it goes anywhere. -->

# When a computer-use agent says "done", is the record right? Five apps, two agents

We gave two computer-use agents the same data-entry tasks in five
open-source business apps and checked every result against the app's own
database. Claude Sonnet 5.5 said "done" while the record was wrong in 23% of
runs (28 of 120, 95% interval 17-32%). GPT-6.1-sol did so in 18% (21 of 120,
12-25%). The intervals overlap, so this does not rank the two.

## What we ran

The apps were Medplum and OpenEMR (health records), Dolibarr (ERP),
EspoCRM (CRM) and Redmine (issue tracking), each running locally with
invented records. Each task is one write: register a patient, record an
allergy, add a contact, create an account, or open an issue. The agent works
in the app's own web pages and ends with DONE or NOT DONE. Then an oracle
reads the database.

In most cases something goes wrong between the browser and the app: a value
is changed in transit, the save is delivered twice, the reply is lost, the
save is refused, or another record is changed along with it. The rest are
clean. Each of the 40 public cases ran 3 times per agent. The protocol,
cases and metrics were committed before the first scored run.

## What we found

- **No false "done" on clean tasks.** Neither agent said done with a wrong
  record when nothing interfered (0 of 30 each).
- **Changes to other records go unnoticed.** When a save also changed a
  different, existing record, the agents said done every time they finished
  the task. Nothing on the task's own screens shows that change.
- **Duplicates are mostly missed.** When a save landed twice, Claude said
  done with two records in 10 of 15 runs, GPT-6.1-sol in 7 of 15.
- **Changed values are mostly caught.** When the save altered a value, both
  agents usually saw the saved record and fixed it.
- **A lower rate is not always a better agent.** GPT-6.1-sol's lower rate
  comes from saying done less often (55% of runs, against 88%). When it did
  say done, the record was wrong 32% of the time, against 26% for Claude.
  Part of that gap is our own task wording: in OpenEMR, our task said "sex"
  while the app's required field is labelled "Birth Sex", and GPT-6.1-sol
  refused to fill a field it thought it wasn't asked to touch.

We also kept 15 cases private, so the public set can't be tuned against.
Both agents scored 20% there (9 of 45), inside their public intervals.

## Limits

Two agents only: we couldn't run Gemini or Amazon Nova this time. The
samples are small and the faults are injected, so these numbers describe
this setup, not how often such faults happen in practice. We build tools
that check agents' writes, which is a conflict of interest; the harness and
every public run are published so anyone can rerun it with their own agent.

The full results, protocol and run data are in the OpenAdapt repository
under `packages/flow/benchmark/write_leaderboard_v2/`.
