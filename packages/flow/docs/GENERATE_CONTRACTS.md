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
  flavor is `create` and `found` for a kind that adds a record, `update` for a
  kind that changes one.
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
| Equal after case folding (an email address) | `Ana@Example.org` |
| Equal digits (a phone number) | `02 38 53 21 09` stored as `0238532109` |
| Equal as a number, a date or a date-time, compared in SQL | `9.20` stored as `9.2`; `2026-10-27 14:30` stored as `2026-10-27 14:30:00` |
| Minutes between two date-time columns of the row | a call's duration |
| A display column of the row a key column points to | `project_id` -> `projects.name` |
| Two text columns joined by one space | `firstname` and `lastname` |
| A row the write added that holds the main record's key | a journal note, a custom field value |

A key column can be a declared foreign key, or a column whose value is
another table's primary key. When several places fit in every demonstration,
the generator keeps the shortest path, then a declared key, then a column
named after its table, then the most exact rule. If places are still tied,
that is a question.

An update parameter that no example gives, such as `new_zip`, is placed at the
main table's column with the same name after a leading `new_` or `set_`, when
there is one. The report lists such parameters under `placed_by_name`.

**Field sets.** A task's field set is its non-null values. For an update, the
generator writes a contract for every non-empty set of the fields to change
that it found, up to 64. For a create, it writes one per demonstrated set,
plus the sets with an optional field (null in some example) left out. A
field left out of a create must be empty in the new record. A list parameter
gets a contract per length, from 1 to 10 items.

**Create, found, update.**

- `create`: exactly one new main record holds the task's values, and
  everything else in the new row is what the demonstrations showed, or is
  bookkeeping.
- `found`: the record already exists, and nothing in the database changed.
  `choose` reads the store first and picks `found` when a record with the
  task's values is already there.
- `update`: exactly one record matches the task's identity values and holds
  the new values. Only the fields the task changes may change.

**Rows the app adds on its own.** A row type the app added the same number of
times in every demonstration (an agenda event, a stream note, a price-history
row) is declared with that count and its constant columns. A table whose
added rows vary between demonstrations, or whose name says log, history,
audit or similar, is proposed as a log. Columns that changed with no task
value (a modified time, a version counter) are proposed as bookkeeping.
Both go in one question.

**The frame.** On MySQL and MariaDB every contract uses the whole-database
frame (`frame.database`): a row added, removed or changed in any table that
the contract doesn't declare stops the write. The main record's table and
the tables of rows that hold values are read in full by the contract's own
reads, with every column. On PostgreSQL, `frame.database` isn't available, so
every other table is read as its own surface; tables proposed as logs are
not read there.

**The recheck window.** On MySQL and MariaDB, one question asks how long the
app can still write on its own after a verified write. The answer is
`frame.database.recheck_after_s`; the proposal is 300 seconds.

## Setup steps it applies

The generator never leaves a step for a person to apply later. A contract
that needs something the store doesn't have is either set up, with a flag
and an admin connection you give, or not written, with the reason.

- `--grant-trigger --admin-read admin.yaml --reader-account "'oa_reader'@'%'"`:
  grants `TRIGGER` on the database to the read account (MySQL, MariaDB).
  Without it, the generator stops and prints the `GRANT` statement for the
  DBA. Note that `TRIGGER` also lets the account create triggers. The verifier
  needs it only to see them.
- `--create-views --admin-read admin.yaml --reader-account ...`: the
  verifier's SQL guard refuses some table names in a read (EspoCRM's `call`).
  This creates a view `oa_read_<table>` and grants the read account `SELECT`
  on it. Without the flag, the kind is refused with the reason.

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
  task values (an invoice's computed totals, its draft reference) are not
  checked. The bookkeeping question lists them.
- On PostgreSQL there is no whole-database frame and no recheck: tables
  proposed as logs are not read, and a write that lands after the check is
  not seen.
- REST metadata (OpenAPI) is not read yet. Apps without database access need
  hand-written contracts.
- The generator does not read an audit trail, so another user's edit during
  the window stops the write instead of being attributed to them.
