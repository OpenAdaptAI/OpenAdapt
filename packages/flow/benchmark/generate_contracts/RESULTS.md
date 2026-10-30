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
| Questions for a person | **3** | **1** | **2** |
| First study, person decisions | 11 | 11 | 11 |
| Contracts written | 93 | 95 | 235 |
| Correct writes confirmed (tasks and controls) | 40/43 | 35/38 | 35/40 |
| In-scope planted faults stopped | **22/22** | **19/19** | **21/21** |
| First study, in-scope faults stopped | 21/21 | 26/26 | 16/19 |
| Out-of-scope late writes stopped | 2/2 (recheck) | 0/2 (no recheck on PostgreSQL) | 1/2 (recheck) |
| `propose` time | 1.5 min | 2.5 min | 2 min |

The first study measured false stops on agents' writes; here the correct
writes are the pack's reference writes and the controls, so the two false
stop rates are not the same measure.

### Questions asked

- **Dolibarr** (3): bookkeeping (agenda events per write, `tms`,
  `fk_user_modif`, the `socialnetworks` rewrite, invoice totals and draft
  reference not checked); the recheck window (300 s proposed and accepted);
  `invoice_date` is in `datef` and in `date_lim_reglement` (picked `datef`).
- **Redmine** (1): bookkeeping (custom field rows per write, `journal_details`
  as a log, `updated_on`, `lock_version`, `user_preferences.others`, and the
  time entry's `tyear`, `tmonth`, `tweek`). No recheck question: PostgreSQL
  has no whole-database frame.
- **EspoCRM** (2): bookkeeping (stream notes, `call_user` and email link rows per write; `action_history_record`, `auth_log_record` and others as logs; `stream_updated_at`, `modified_at`); the recheck window (300 s).

Every answer was the proposal, except the one choice in Dolibarr's question 3.

### Setup steps the generator applied

- Dolibarr and EspoCRM: `GRANT TRIGGER ON <db>.* TO <read account>`
  (`--grant-trigger`): the whole-database frame must see triggers.
- EspoCRM before the SQL guard fix (#1665): a view `espocrm_oa_read.oa_read_call` (`--create-views`), because the guard refused the table name `call`. After the rebase the guard reads `call` and no view is needed.

### False stops, by cause

| App | Case | Cause |
| --- | --- | --- |
| Dolibarr | F18 (control) | Another user's edit to an unrelated customer: no audit trail is read, so it isn't attributed |
| Dolibarr | F21 (control) | Two saves add two agenda events; the contract expects one per write |
| Dolibarr | F30 (control) | A resave of a value already in place adds an agenda event; the `found` contract allows no change |
| Redmine | T19 | Closing an issue sets `closed_on`; no demonstration closed one |
| Redmine | F21 (control) | Another user's edit to an unrelated issue: not attributed |
| Redmine | F24 (control) | Two saves add two journals |
| EspoCRM | T21 | The reference write removes the old email address row when replacing it; the demonstration didn't |
| EspoCRM | T22 | `field: phone` has no contract: no demonstration, and phones live in another table |
| EspoCRM | F22, F27 (controls) | Another user's unrelated edit or new lead: not attributed |
| EspoCRM | F24 (control) | Mixed-case email and padded name (emails now compare case-folded) |

### Misses

Out of scope only: Redmine F19 and F20 (late writes; PostgreSQL has no
recheck). EspoCRM F21 (a late edit to a look-alike lead: an update's main table is read by its own surface, which the recheck doesn't read). No in-scope planted fault was confirmed on any app.

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

Code: these runs used this branch before the rebase onto `main`, with #1661
(the whole-database frame fix) merged in, and before `frame.versions` was
emitted. Reruns on the rebased branch are in progress.
