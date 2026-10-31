# Generate verify contracts from a SQL schema

`python -m openadapt_flow.generate.verify_contract` writes the
`openadapt.verify/v1` contracts for an app's kinds of write. You give it a
read-only database account, a short description of each kind of write, and
one demonstration write per example. It asks a person only what it can't
decide alone, in a numbered list. The goal is three questions or fewer per
app.

Use it for any app whose data is in MySQL, MariaDB or PostgreSQL. For a FHIR
server, use `openadapt_flow.generate.ensure_contract` (an
`openadapt.ensure-record/v1` contract). This guide needs no code reading.

## What you need

- **A read-only database account.** The contracts read with it, and so does
  the generator. Put its connection in `read.yaml`, with the password in an
  environment variable:

  ```yaml
  sql_driver: pymysql            # or psycopg2 for PostgreSQL
  sql_connect_args: {host: 127.0.0.1, port: 3306, user: oa_reader, database: app}
  sql_password_env: APP_READER_PASSWORD
  ```

  The generator refuses an account that can insert, update, delete or change
  the schema. On MySQL and MariaDB the account also needs the `TRIGGER`
  privilege, because the whole-database check must see triggers. The
  generator can grant it (see [Setup steps it applies](#setup-steps-it-applies)).
- **`kinds.yaml`**: each kind of write, its parameter names, and one or two
  example tasks. A null value means the task leaves that field out. A list
  value (invoice lines) is a list of mappings.

  ```yaml
  kinds:
  - kind: update_customer
    parameters: [customer, town, new_phone, new_email, new_address, new_zip]
    examples:
    - values: {customer: Transports Petit, town: Rouen, new_phone: 02 35 15 60 60}
    - values: {customer: Pharmacie Moreau, town: Lille, new_phone: 03 20 06 07 08,
               new_email: officine@pharmacie-moreau-lille.fr}
  ```

- **The acting user's id**, as the caller passes it to the verifier, for
  example `--actor-param bot_user_id=5`. A column that holds it (an author or
  creator column) is checked against it.

## Steps

1. Check the account and read the schema:

   ```bash
   python -m openadapt_flow.generate.verify_contract schema --read read.yaml
   ```

2. For each example, make the write once, correctly, the way the agent will
   (through the app's API or screens), between a snapshot and a record:

   ```bash
   python -m openadapt_flow.generate.verify_contract snapshot --read read.yaml --out work/before.json
   # ... make the write ...
   python -m openadapt_flow.generate.verify_contract record --read read.yaml \
     --before work/before.json --kind update_customer \
     --values '{"customer": "Transports Petit", "town": "Rouen", "new_phone": "02 35 15 60 60"}' \
     --demos work/demos
   ```

   Skip an example whose record already exists ("make sure X exists"): the
   right outcome there is no write. Snapshots hold row values. Keep the work
   folder private, like a database export.

3. Propose:

   ```bash
   python -m openadapt_flow.generate.verify_contract propose --read read.yaml \
     --kinds kinds.yaml --demos work/demos --out work/out --actor-param bot_user_id=5
   ```

4. Read `work/out/QUESTIONS.md`. Write the answers in `answers.yaml` and run
   `propose` again with `--answers answers.yaml`. Until every question is
   answered, the manifest says so and `choose` refuses to pick a contract.

## What it writes

- `contracts/<kind>/<fields>.<flavor>.yaml`: one contract per field set. The
  flavor is `create` or `update`, and each has a `found` twin for a task
  whose record already holds its values.
- `manifest.yaml`: which contract fits which task values.
- `QUESTIONS.md`: the numbered questions, the proposed answers, and per kind
  how many contracts were written and why any were not.
- `report.json`: where each parameter was found, the rows the app adds on its
  own, the tables treated as logs, and every contract that was not written,
  with the reason.

At run time, pick the contract for a task with `choose`, then call the
verifier as usual:

```python
from openadapt_flow.generate.verify_contract import choose
from openadapt_flow.verifier import Actor, begin_write, verify_write

contract, params = choose("work/out/manifest.yaml", "update_customer", task_values,
                          {"bot_user_id": "5"})
pending = begin_write(contract, params, actor=Actor("agent", "bot"))
...  # the agent writes
result = verify_write(pending)
```

`choose` raises `GenerateError` when no contract fits the task's values. Treat
that as a stop: the write must not be sent unchecked.

## How it decides

**The main record.** In each demonstration, the main record is the added row
(a create) or changed row (an update) that holds the most task values.

**Where a value is.** Each value is looked up by exact rules only:

| Rule | Example |
| --- | --- |
| The stored text equals the value | `subject` |
| Equal after case folding, for a value shaped like an email address | `Ana@Example.org` |
| Equal digits, only for a parameter a person confirmed as a phone number (the `phones` question), when the app stored other text with the same digits | `02 38 53 21 09` stored as `0238532109` |
| Equal as a number, a date or a date-time, compared in SQL | `9.20` stored as `9.2`; `2026-10-27 14:30` stored as `2026-10-27 14:30:00` |
| Minutes between two date-time columns of the row | a call's duration |
| A display column of the row a key column points to | `project_id` -> `projects.name` |
| Two text columns joined by one space | `firstname` and `lastname` |

A display value behind a key must name exactly one row of its table that
isn't soft-deleted (a `deleted` or `is_deleted` flag, or a `deleted_at`
time): a second "Jon Park", or "Mary Ann Lee" split as `Mary Ann|Lee` and
`Mary|Ann Lee`, makes the value empty, so the write stops. At run time a
confirmed phone value that isn't written like a phone number (fewer than 7
digits, or other characters) is compared exactly.
| A row the write added that holds the main record's key | a journal note, a custom field value |
| A row it added that links the record to the row holding the value | an email address, a call's attendee |

A key column can be a declared foreign key, or a column whose value is
another table's primary key. When several places fit in every demonstration,
the generator keeps the shortest path, then a declared key, then a column
named after its table, then the most exact rule. Among places still tied, it
keeps, in this order:

1. the column named like the parameter (`stage`, not `last_stage`);
2. the source column when another is its lower-cased or digits-only copy in
   every row (`email_address.name`, not `email_address.lower`), or the column
   the table defines first when they hold the same value in every row;
3. the place another kind's demonstrations used for the same table.

If places are still tied, that is a question. A value that is also in a row
the write added beside the record (a call's lead is its parent and an
attendee row) is checked in both places.

A parameter that no example gives is placed where another kind's
demonstrations found the parameter of the same name on the same main table
(`placed_from_another_kind`), else at the main table's column of the same
name after a leading `new_` or `set_` (`placed_by_name`). A parameter whose
value names the field to change (`field: status`, `value: In Process`) gets a
contract per field: the demonstrated ones, and every other text column of the
main table.

**Field sets.** A task's field set is its non-null values. For an update, the
generator writes a contract for every non-empty set of the fields to change
that it found, up to 64. For a create, it writes one per demonstrated set,
then the sets with optional fields left out, up to 64 in all. A field is
optional when some example leaves it null or its place can be empty (a
nullable column, a row beside the record). A field left out of a create must
be empty in the new record, unless the demonstrations that left it out
filled that column anyway (a call's parent, which is its account when there
is no lead). A row the app adds with an empty value when a field is left out
(Redmine's custom field values, a journal with no note) is expected then
too. A list parameter gets a contract per length, from 1 to 10 items.

**Create, found, update.**

- `create`: exactly one new main record holds the task's values, and
  everything else in the new row is what the demonstrations showed, or is
  bookkeeping.
- `found`: the record already holds the task's values and, for a create,
  the same constants the demonstrations' new rows held (a soft-delete flag, a
  status, a type), and nothing in the database changed. `choose` reads the
  store first and picks `found` when such a record is already there: a "make
  sure it exists" task whose record exists, or an update that is already in
  place. A trashed record with the task's values is not found.
- `update`: exactly one record matches the task's identity values and holds
  the new values. Only the fields the task changes may change, plus the
  target's own bookkeeping columns a person confirmed. When the
  demonstrations replaced the record's rows beside it (an old email address
  link removed), the earlier rows may go, and afterwards the record has as
  many such rows as the demonstrations left.

**Columns of a new row no value explains.** Each is pinned to the acting
user, to the value every demonstration held, or to a copy of a column of the
row a key points to (an invoice line's price from its product). A key, a
link to a row the write added, the year, month, week or day of a date the
task gives, and a generated token (a UUID) are recognized as such. Columns
that differ between demonstrations, free text every demonstration left
empty, and the write's time stamps are listed in the bookkeeping question,
and each is left unchecked only if the answer names it (`unchecked`). Free
text the answer doesn't name must stay empty; a kind with any other column
the answer doesn't name gets no contract. The proposed answer names every
listed column, so accepting it is explicit.

**Rows the app adds on its own.** A row type the app added the same number of
times in every demonstration (an agenda event, a stream note, a price-history
row) is declared with that count and its constant columns. A table whose
added rows differ in number between demonstrations is listed in the
bookkeeping question and is a log only if the person names it (`logs_add`);
otherwise every added row stops the write. The person may also name a
counted table (an access log the app writes once per request: the agent may
make more requests than the demonstration did). A named table is a log only
for the kinds whose demonstrations added rows there: rows may be added to
it, and existing rows must not change. The generator never decides a table
is a log from its name.

**Changes the app makes to existing rows.** A column of an existing row that
the demonstrations changed with no task value is excused only on that row:
the target itself, a row with the same key in every demonstration (a counter
row), or the row the task's values name through a key of the main record
(the account a contact belongs to). It is never excused by name on other
rows or tables. Time stamps set during the write, counters that went up and
columns set to the acting user are proposed; anything else is listed and
excused only if the person adds it (`columns_add`). A changed row the
generator can't identify is listed too, and a write that changes it stops.
Each declared row may change only its own table's excused columns
(`frame.target_fields_by_surface`): a parent row the app touches can't take
the target's changed fields, and the target can't take the parent's.

**Answers.** `bookkeeping: accept` takes the proposal. A mapping may have
`accept: true` (take the proposed columns), `logs_add`, `columns_add`,
`columns_remove` and `unchecked`, and each can name only what the question
listed. Any other key, or a table or column the question didn't list, is
refused. A mapping without `accept: true` takes only what it names.

**The frame.** Every contract uses the whole-database frame
(`frame.database`, with the store's dialect): a row added, removed or changed
in any table that the contract doesn't declare stops the write, except rows
added to a table the person named as a log. The main record's table and the
tables of rows that hold values are read by the contract's own reads, with
every column. On PostgreSQL the read account also needs `SELECT` on the
sequences (see [VERIFY_RUN.md](VERIFY_RUN.md)); without it the dry read
fails and no contract is written.

**Lost updates.** An update's contracts set `frame.versions`, so another
user's save to the target inside the window stops the write. The generator
first takes a whole-number column named as a version that went up by one in
every demonstration (Redmine's `lock_version`): every existing record has a
version there. Otherwise it takes the app's own audit trail when every update
demonstration added a row there that names the record and the acting user
(EspoCRM's `action_history_record`, Dolibarr's agenda events). The read is narrowed to the record's type and to write events: a
text column, not an id, that holds one code of letters and underscores in
every row any update kind added (so it doesn't vary with the fields saved)
and only other codes in the rows create kinds added (EspoCRM's `action`).
The read keeps the update code and the create codes, so a record the app
created has a first version, and another user only viewing the record
doesn't count. A time stamp or a token the demonstrations happened to share
is never used. Only the main record is a target (`target_surface`). A target
the read before the write doesn't show as new is treated as existing, and
with no version before the write it stops as `missing` (a record saved
before the trail began, or outside the app). With neither, the update has no
version check, and `report.json` shows `versions: null`. If the app stops
writing the trail (an agenda setting turned off), the agent's own save has
no new version, and the verifier stops the write rather than pass it.

**The recheck window.** One question asks how long the
app can still write on its own after a verified write. The answer is
`frame.database.recheck_after_s`; the proposal is 300 seconds.

## Setup steps it applies

The generator never leaves a step for a person to apply later. A contract
that needs something the store doesn't have is either set up, with a flag
and an admin connection you give, or not written, with the reason.

The read account must be read-only. On PostgreSQL the check uses its
effective privileges, so a role it is a member of counts: `INSERT`,
`UPDATE`, `DELETE`, `TRUNCATE`, `TRIGGER` or `REFERENCES` on any table,
`USAGE` or `UPDATE` on a sequence, or `CREATE` on a schema or the database
stops the generator.

- `--grant-trigger --admin-read admin.yaml --reader-account "'oa_reader'@'%'"`:
  grants `TRIGGER` on the database to the read account (MySQL, MariaDB). The
  account name is split into user and host and escaped by the driver.
  Without it, the generator stops and prints the `GRANT` statement for the
  DBA. `TRIGGER` also lets the account create and drop triggers; the verifier
  needs it only to see them, and `report.json` says so under `read_account`.
  Keep that account's password away from the agent.
- `--create-views --admin-read admin.yaml --reader-account ...`: the
  verifier's SQL guard refuses some table names in a read (EspoCRM's `call`).
  This creates a view `oa_read_<table>` in a schema of its own,
  `<database>_oa_read` (so the app's own restores and upgrades don't drop it),
  and grants the read account `SELECT` on that schema. Without the flag, the
  kind is refused with the reason. `report.json` lists what was applied under
  `applied`.

Every contract is loaded and read once against the live store before it is
written out. A contract whose read fails is not written; `report.json` says
why.

## Limits

- A parameter that no example gives is placed only by the name rule above.
  Otherwise a task that gives it has no contract and is stopped. Add an
  example and a demonstration.
- A value the demonstrations hold in no row, or in a place none of the rules
  describe, is a question.
- A list item that appears twice in one task (the same product and quantity
  twice) is stopped: each item must match one new row.
- Columns of a new record that differ between demonstrations and aren't
  task values (an invoice's computed totals, its due date), free text every
  demonstration left empty, and time stamps are not checked. The bookkeeping
  question lists every one of them.
- With one demonstration of a create, every other column must hold the value
  it held then, including one computed from the task's values (a price with
  tax). `QUESTIONS.md` says so; record a second create with other values.
- REST metadata (OpenAPI) is not read yet. Apps without database access need
  hand-written contracts.
- The frame doesn't attribute changes to other users: another user's edit to
  an unrelated record during the window stops the write. Only the target's
  own versions are read from the audit trail (lost updates).
