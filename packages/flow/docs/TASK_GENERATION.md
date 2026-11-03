# Generate task packs for an app

`openadapt_flow.taskgen` turns one description of an app into hundreds of
validated tasks for training or evaluating agents. Each task has its own
effect contract (its oracle), a scripted solution that works through the
app's screens, and planted faults that the oracle must refute. Nothing here
calls a model.

The generator writes no contract logic of its own. Contracts come from
`openadapt_flow.generate.verify_contract` (see
[GENERATE_CONTRACTS.md](GENERATE_CONTRACTS.md)): the app's schema plus two
demonstration writes per kind, made through the same screens the tasks use.

## What you need

- **A profile** (`openadapt.taskgen-profile/v1`, YAML). It names the app's
  record types, the fields its screens show, the operations each record
  allows, the pools that values are drawn from, and the answers to the
  contract generator's questions. App knowledge lives in the profile, not
  in code. The synthetic profiles are in `openadapt_flow/taskgen/profiles/`.
- **The workbench** (`openadapt_flow.taskgen.workbench`): a server-rendered
  web app built from a profile, on PostgreSQL. It has sign-in, search, record
  pages, and new, edit, cancel and merge forms, and it can switch on each
  task's mechanisms (below). The contracts read it through a read-only
  account that the agent never sees.
- **PostgreSQL** with an admin role and a read-only role (`wb_reader`).

## Steps

1. Demonstrate each kind and generate the contracts:

   ```python
   from openadapt_flow.taskgen.contracts import demonstrate_and_propose
   manifest = demonstrate_and_propose(profile, url, workbench, dsn, Path("work/app"))
   ```

   Read `work/app/out/QUESTIONS.md`. Add the answers to the profile's
   `answers:` and run it again until no question is open.

2. Write a pack:

   ```bash
   python -m openadapt_flow.taskgen.generate mockmed --manifest work/mockmed/out/manifest.yaml \
     --out packs/mockmed --per-cell 8 --seed 10000
   ```

3. Validate it:

   ```bash
   python -m openadapt_flow.taskgen.validate packs/mockmed --profile mockmed \
     --pg "host=127.0.0.1 port=9770 user=wb_admin" --workers 2
   ```

4. Assign splits and write the manifests:

   ```bash
   python -m openadapt_flow.taskgen.splits --rules splits.yaml --out release packs/*
   ```

## Kinds

| Kind | The agent | Contract mode |
| --- | --- | --- |
| `create` | adds a record under a person | create |
| `update` | changes a person's contact fields | update |
| `update_child` | changes one field of one of a person's records | update |
| `cancel` | cancels or voids one of a person's records, with a reason | update |
| `merge` | finds a person's duplicate record and merges it into the original | update |
| multi-app | one write in each of two apps, for the same person under different keys | one contract per app |

A reconcile task is an `update_child` or `update` whose value is on a lookup
record (the `hidden_state` family). A multi-app flow is a data file in
`openadapt_flow/taskgen/flows/`.

## Families

Each family changes the task's seed data, the app's behavior during the
episode, and the scripted solution. Each family also adds the planted
variants that the oracle must refute.

| Family | What is hard | Planted variant |
| --- | --- | --- |
| `control` | nothing | an app fault |
| `lookalike_id` | another person's key differs by two swapped digits | the write on the look-alike |
| `near_duplicate` | a second person with the same name; the task names the birth date | the write on the namesake |
| `stale_session` | the session has expired by the first save, which is lost | success claimed after the lost save |
| `late_write` | the save lands three seconds later | the late write never lands |
| `misleading_banner` | the first save shows "Saved" and stores nothing | success claimed on the banner |
| `interruption` | a message mid-task corrects a value (or, in the positive form, confirms it) | the value from before the correction |
| `hidden_state` | the value is on a lookup record, next to a decoy record | the decoy's value |
| `concurrent_edit` | a second user edits the record while the agent does | the second user's change lost |

Every task also gets one app fault: a duplicate save, a save on another
record, a partial save, or no save at all. Every task is feasible. The
positive forms keep the cue but need the ordinary write, so the reward
never pays for refusing.

## What validation checks

For each task, on a fresh copy of the app:

- the scripted solution reaches `done` through the screens alone, and the
  task's contract scores it `CONFIRMED`;
- every planted variant scores `REFUTED` or `INDETERMINATE`.

Tasks that fail either check are rejected. `validity.json` reports the
validity rate by app, kind and family, and the reason for each rejection.

## Known limits

- `concurrent_edit` tasks are rejected. The generated contracts read the
  second user's write as collateral: attribution through the audit log
  explains the changed database row, but not the same change seen through
  the contract's own surface reads. These tasks stay out of packs until the
  contract generator can attribute a surface read.
- An update never changes the field that names the record (an encounter's
  note): the contract would look for the old value.
- The contract generator refuses the person kinds on the `mockmed-v2`
  profile (it can't tell whether the audit row's actor column is a constant
  or a copy). That profile's packs hold the record kinds only.
