# Generated contracts on the first study's apps

Written 2026-10-30. The generator (`python -m openadapt_flow.generate.verify_contract`)
was run on three of the first-contact study's apps, each on its own copy:
Dolibarr instance 1, Redmine instance 1 and EspoCRM instance 2. Each run used
only what a new customer has:

- the app's read-only database account (the fixture's `read_access()`);
- the kinds and parameter names, and the example values, from the app's
  `setup_view.yaml`;
- one demonstration write per example, made here through the app's public
  REST API as the bot (`demo_<app>.py`). An example whose record already
  existed was replaced by a create with values of our own.

The study's tasks, oracle and fault scripts were used only afterwards, by
`validate.py`, to judge the contracts: each task's reference correct write
and each planted fault, through `choose`, `begin_write` and `verify_write`.
Late-write faults also ran the recheck the contract declares.

## Results

| | Dolibarr | Redmine | EspoCRM |
| --- | --- | --- | --- |
| Database | MariaDB 11.4 | PostgreSQL | MariaDB |
| Questions for a person | **4** | **2** | **3** |
| First study, person decisions | 11 | 11 | 11 |
| Field sets with a contract pair | 93 | 95 | 235 |
| Correct writes confirmed (tasks and controls) | 40/43 | 32/38 | 27/40 |
| In-scope planted faults stopped | **22/22** | **19/19** | **21/21** |
| First study, in-scope faults stopped | 21/21 | 26/26 | 16/19 |
| Out-of-scope late writes stopped | 2/2 (recheck) | 1/2 (recheck) | 1/2 (recheck) |
| Lost-update versions (`frame.versions`) | agenda events (`llx_actioncomm`, `code = AC_COMPANY_MODIFY`) | journals | `action_history_record` (`action = update`, `target_type = Lead`) |
| `propose` time | 2 min | 0.5 min | 1 min |

Every contract uses the whole-database frame (`frame.database`) with the
store's dialect, on PostgreSQL too, and declares the recheck window.

The first study measured false stops on agents' writes; here the correct
writes are the pack's reference writes and the controls, so the two false
stop rates are not the same measure.

### Questions asked

- **Dolibarr** (4): which parameters are phone numbers (both proposed,
  both confirmed); bookkeeping (agenda events per write, `tms`,
  `fk_user_modif`, invoice totals and draft reference not checked; the
  person added the `socialnetworks` rewrite under `columns_add`); the recheck
  window (300 s); `invoice_date` is in `datef` and in `date_lim_reglement`
  (picked `datef`).
- **Redmine** (2): bookkeeping (custom field rows per write, `updated_on`,
  `lock_version`, the time entry's `tyear`, `tmonth`, `tweek`; the person
  named `journal_details` a log and added `user_preferences.others`); the
  recheck window (300 s).
- **EspoCRM** (3): which parameters are phone numbers (confirmed);
  bookkeeping (`stream_updated_at`, `modified_at`, `call_user` and stream
  note rows; the person named `action_history_record` and `auth_log_record`
  logs, and left `note` and `note_user` out, so stream notes are counted);
  the recheck window (300 s).

The generator decides no log by table name. Every log was named by the
person in the bookkeeping question.

### Setup steps the generator applied

- Dolibarr and EspoCRM: `GRANT TRIGGER ON <db>.* TO <read account>`
  (`--grant-trigger`): the whole-database frame must see triggers.
- Redmine: none. The read account needs `SELECT` on the sequences too,
  which the study's fixture grants.

### False stops, by cause

| App | Case | Cause |
| --- | --- | --- |
| Dolibarr | F18 (control) | Another user's edit to an unrelated customer: not attributed |
| Dolibarr | F21 (control) | Two saves add two agenda events; the contract expects one per write |
| Dolibarr | F30 (control) | A resave of a value already in place adds an agenda event; the `found` contract allows no change |
| Redmine | T19 | Closing an issue sets `closed_on`; no demonstration closed one |
| Redmine | T22, T23, T28 | The issue subject names issues in two projects: a display value must name exactly one row |
| Redmine | F21 (control) | Another user's edit to an unrelated issue: not attributed |
| Redmine | F24 (control) | Two saves add two journals |
| EspoCRM | T19, T23 | A status change adds a stream note; no demonstration changed the status |
| EspoCRM | T21 | The reference write removes the old email address row when replacing it; the demonstration didn't |
| EspoCRM | T22 | `field: phone` has no contract: no demonstration, and phones live in another table |
| EspoCRM | T29 to T34, T36 | Logging a call adds stream notes for each linked record and touches the contact's `stream_updated_at`, in numbers the demonstrations didn't show. `note` isn't a log unless the person says so |
| EspoCRM | F22, F27 (controls) | Another user's unrelated edit or new record: not attributed |

The stream-note stops are the price of not calling `note` a log: a person
who names it adds those rows to the logs, and an extra stream note (a
planted fault) would then pass.

### Misses

Out of scope only: Redmine F20 and EspoCRM F21. Both are late writes to the
main record's table, which the contract reads through its own surface; the
recheck reads the rest of the database again but not that surface. No
in-scope planted fault was confirmed on any app.

## Reproduce

```bash
cd packages/flow/benchmark/generate_contracts
FCG_INSTANCE=1 FCG_DOLIBARR_READER_PASSWORD=... python demo_dolibarr.py WORK
python -m openadapt_flow.generate.verify_contract propose --read WORK/read.yaml \
  --kinds WORK/kinds.yaml --demos WORK/demos --out WORK/out \
  --actor-param bot_user_id=2 --answers WORK/answers.yaml \
  --grant-trigger --admin-read admin.yaml --reader-account "'fcg_reader'@'%'"
FCG_INSTANCE=1 python validate.py dolibarr WORK --actor-id 2
python summarize.py dolibarr WORK
```

`results/<app>/` holds each run's `QUESTIONS.md`, `answers.yaml`,
`report.json`, `manifest.yaml` and two example contract pairs per app (the
full set is regenerated by the commands above). The fault scripts and the
oracle stay in the study's private pack.

Code: the final runs used this branch rebased on `main` (with #1661 to
#1669 and #1673): Redmine and EspoCRM at `0bd47c616`, Dolibarr at
`f4cb759f9`. The commit after narrows the versions filter to letter-only
codes; the three apps' versions reads are the same under it.
