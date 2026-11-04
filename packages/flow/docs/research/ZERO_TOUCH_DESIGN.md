# Zero-touch setup: design

Written 2026-11-03. Goal: setting up the write verifier for a new app needs
**no person decision** and finishes in minutes, and the verifier is no
easier to fool than with a person's setup. The evidence behind each choice
is in [ZERO_TOUCH_ONBOARDING.md](ZERO_TOUCH_ONBOARDING.md); bracketed tags
like [C5] point there.

## The rule

> An agent proposes every piece of setup as data. A machine admits a piece
> only after it confirms held-out correct writes and stops planted faults,
> in a disposable copy of the app's store. Anything it can't admit is not
> excused: those tasks stop, and the piece is listed for a person to look
> at. That list is the measure, and its target is zero.

A person is never asked a question during setup. They may read the list.

## Where person decisions came from

The retests' setup logs list each decision. Grouped by type, with what
replaces each:

| Decision type | Today | Zero-touch replacement | Checked by |
| --- | --- | --- | --- |
| Read-only account, a `TRIGGER` grant, a view | A person runs SQL | The deployer gives one admin connection once (an input, not a judgment); the setup applies grants and views itself, as the generator's `--grant-trigger` and `--create-views` already can | The generator's read-only check |
| Bookkeeping: which changed columns are the app's own | A person answers the generator's question | The generator's own proposal, unchanged, plus `now_checks` (an excused time stamp must be near the read). Nothing is added: no `logs_add`, no `columns_add` | Bookkeeping faults planted in every changed cell; an unchecked cell fails its kind |
| Recheck window | A person picks seconds | A policy constant (300 s) | Late-write faults |
| Phone-number comparison | A person confirms | Every listed parameter | Wrong-digit faults |
| Where a field is stored, when two places fit | A person picks | Try each candidate; keep the one whose kind validates, only if exactly one does | Held-out writes and wrong-value faults |
| Which demonstrations to make | A person chooses values | The onboarding agent makes them, with values chosen to differ (different dates, list lengths 0, 1 and 2, optional fields on and off), plus held-out writes it never shows the generator | Held-out writes |
| How a task's value is stored (`high` as 3, a flag as text) | A person writes a mapping | The agent writes the mapping as exact code | Every demonstration and held-out write must place every parameter through it |
| Hand edits to contracts | A person patches YAML | None. A kind that needs one isn't admitted | Listed in the exception report |
| Shadow routes (which request feeds which contract) | A person maps them | The agent proposes them from recorded traffic | A route that doesn't place every parameter of a held-out write is refused |
| Learned excuses | A person approves the proposal | Admitted per excuse by planted faults at its edge: the same column on another user's row, another column on the actor's row, one more row than `max_rows` | Those faults must stop |
| Identity region on screen | A person picks | Candidates whose label is fixed and value varies across recordings | The region must read the right record on every recording and the wrong one on a different record (a negative test) |

## Pipeline

```
schema + demonstrations ──► generator propose ──► questions
                                   ▲                  │
                                   │          auto-answer by rule
                                   │                  │
                     propose again with answers ◄─────┘
                                   │
                                   ▼
            sandbox copy ◄── held-out writes + planted faults
                                   │
                    admitted kinds ─┴─ exception report
                                   │
                    production contracts (same answers)
```

1. **Sandbox.** A copy of the app's tables beside the app's own, made with
   the admin connection: a database `<db>_oa_sandbox` (MySQL, MariaDB) or a
   schema `oa_sandbox` (PostgreSQL). The read-only account is granted read
   on it. The setup refuses to write or drop anything not named like a
   sandbox. Faults are never planted in the app's own tables.
2. **Propose.** The existing generator, pointed at the sandbox, with the
   training demonstrations. No answers.
3. **Answer by rule.** The table above. Each answer goes in a ledger with
   the rule that produced it.
4. **Propose again** with the answers.
5. **Validate.** For each held-out write: put its rows back as they were
   before it, choose the contract, `begin_write`, put the write's rows (its
   own time stamps moved to now), `verify_write`, then the recheck. It must
   be `CONFIRMED`. Then, from the same write, plant faults (below); each
   must end not `CONFIRMED`. Every touched row is put back after each case.
6. **Admit** a kind when every held-out write of it is confirmed and every
   fault stopped. Write the production contracts with the same answers
   against the app's own read account, and drop every kind not admitted
   from the manifest, so `choose` finds no contract and its tasks stop.
7. **Report** `ONBOARDING.json` (minutes, answers, ledger, per-kind
   results) and `EXCEPTIONS.md`.

## The faults

Built only from the held-out write's recorded diff, the schema and the
task's values. The planter never reads a contract.

Class names follow `openadapt_flow.fault_gate` where the class is the same.

| Class | What is planted |
| --- | --- |
| `missing` | Nothing is written |
| `partial` | One row of a multi-row write is lost (a child row, a journal entry) |
| `wrong_value` | A cell holding a task value gets another value |
| `bookkeeping` | A cell holding no task value gets another value, NULL cells included |
| `collateral_target` | A column of the target the write didn't change is changed |
| `wrong_record` | An update's changes land on another row of the same table (a copy of the target when the table has no other row) |
| `look_alike` | A create's values land on an existing row instead |
| `duplicate` | Each added row is added again under a new key |
| `unrelated_edit` | Each column the update changed is changed the same way on another row; and one other row of each touched table is edited |
| `other_table` | A row is added to a table the write didn't touch |
| `delayed` | The write is correct; a change to the target, or to another row, lands after the check and before the recheck |

**Every column, no cap.** Every column of every row the write adds or
changes gets its own fault. The value planted is, in order: another row's
key for a declared foreign key; a value the column already holds in another
row (so the store accepts it); or a value made by the column's type (a date
in 2001, a number plus one, text of the same length with its last character
changed, valid JSON, a new UUID, a flipped byte, another time). A column
whose type has no generator gets an `unplantable` case.

**Only a verdict counts.** A fault counts as stopped only when the verifier
read the planted state and returned `REFUTED` or `UNCERTAIN` (at the check
or the recheck). A plant the store rejects, a case that errors, a fault
with no contract and an unplantable column all count as not stopped, so the
kind is not admitted.

**A failed read is not a verdict.** An `UNCERTAIN` that came from a read
the verifier couldn't make (a timeout, a lost connection, rows still
moving) is run again, up to three times; if the read keeps failing, the
case is `READ_FAILED` and doesn't count as stopped.

**A duplicate the store forbids.** A `duplicate` the store refuses counts
as not applicable only when the refusal is a unique violation of an index
the catalog declares unique (not the primary key) and every column of that
index holds the same value in the copy as in the row copied. Any other
refusal is a failure. A kind whose every duplicate is refused this way
records "duplicates impossible by constraint" with the index names instead
of a planted duplicate. Other plants avoid declared unique indexes, so the
store takes them.

**Only validated field sets.** A zero-touch manifest lists only the field
sets a held-out write validated (built during setup when needed) and builds
none at run time: a task with another field set stops.

**Two readings, both checked.** The setup answers `strict_pins: true`, so a
new-row column two readings fit in every demonstration (a constant and a
copy, the acting user and a copy, or one demonstration's one value) is
checked both ways instead of refusing the kind. A `found` contract (the
record is already there) keeps only the constant half: who made the record
may differ.

**Required classes.** A kind is admitted only if its held-out writes
produced `missing`, `unrelated_edit`, `delayed` and `other_table` faults,
plus `duplicate` and `look_alike` for a create and `wrong_record` for an
update. A class that couldn't be planted is listed in `EXCEPTIONS.md`.

**Held out means held out.** A held-out write with the same kind and values
as a demonstration, or the same diff, is refused, and so is a kind with no
held-out write.

**Production equals validated.** The sandbox copies the declared foreign
keys, because the generator reads them. The production contracts are
proposed again against the app's own store; a kind is kept only if its
contracts and manifest entry match the validated ones once connections and
the recheck window are set aside. The manifest is filtered (entries, pinned
digests and files) in a staging folder and moved into place in one rename.

The `bookkeeping` class is what keeps excuses honest. Every cell the write
changes is either a task value (checked), a value a rule checks (the acting
user, a constant, a copy, the current time), or an excuse. A fault planted
in an excused cell with no value rule passes, so its kind is not admitted.
That turns "is this column bookkeeping?" from a judgment into a test.

## Keeping validation independent

The fault planter and the contract generator must not share blind spots
[B19, B20]. Four things keep them apart:

1. **Different method.** The generator infers places and roles from
   demonstrations. The planter mutates every cell and row the held-out
   write touched, plus rows it didn't, by fixed operators. No model writes
   either side.
2. **Different input.** The planter reads the held-out diff, never the
   contract or the generator's report.
3. **Held-out writes.** The generator never sees the writes it is
   validated on [A8].
4. **Hidden faults written by someone else.** For measurement, the
   pre-registered packs' faults (written by separate pack authors, frozen
   before setup) are run against the admitted contracts. They answer pass
   or fail, and are never shown to the setup [C18, C19].

An LLM fault generator can be added on top later, from a different model
family and a separate prompt, but only above this floor [C12].

## What this reuses, and what it doesn't

**The fault gate** (`openadapt_flow/fault_gate.py`, #1445) runs a fixed
suite against a test copy and writes a coverage record that `verify-run`
reads under an `admission:` policy. Zero-touch uses its class names and its
two-sided rule (the clean save must pass, every fault must fail). It does
not call `run_gate`, for two reasons:

- the gate plans its faults from the check's declared fields
  (`_declared`), so it can't show that a check catches a change to a cell
  the check doesn't declare. That is the blind spot an over-wide excuse
  hides in [C5];
- its test copy addresses one table slice by one key field; a generated
  contract judges the whole database.

A follow-up can let `run_gate` take a plan derived from a recorded diff and
write the same coverage record, so one admission format covers both.

**Draft PR #1520 (`generate-parts`)** generated four workflow parts, each
admitted by its own check, then approved by a person. What carries over:

| #1520 part | Here |
| --- | --- |
| The floor (`baseline.py`): generation adds checks, never removes one | Adopted as a rule: zero-touch only answers the generator's questions with its own proposal and adds `now_checks`; it never names a log or an extra column |
| Read channel from OpenAPI or a CapabilityStatement, admitted by a channel fault test (401, 500, a truncated or non-JSON body, a failed next page all read as unreadable) | The admission rule for read access to an app with no database account (REST, FHIR). Not needed for the SQL development apps; the code stays on the branch for that step. Its pagination fix already landed in #1538 |
| Record contract from diffs plus the fault gate | Superseded for SQL stores by the contract generator and this sandbox |
| Input types: a code table learned one entry per demonstrated value, any other value halts, admitted by a round trip | The admission rule for value mappings (`high` stored as 3): an exact table from the demonstrations, an unknown value stops, every held-out write must round-trip |
| Identity onto a reviewed template, with a perturbation suite where each slot has a case only it can catch | The model for record identity of people (patients, customers): the template is reviewed once per domain, never per app; the mapping is generated and admitted by perturbation |
| A person's approval bound to the manifest digest | Replaced by the exception report. Nothing is approved |

#1520 itself is closed in favour of this work; its branch stays for the
channel and identity code when those steps are built.

## What stays general

No app-specific code or tests. Everything per app is data:

- the kinds file, demonstrations and held-out writes (made by the agent
  through the app's own API or screens);
- value mappings (exact code the agent writes, checked mechanically);
- the generated contracts and the answers ledger.

The code paths are the same for every app: the generator, the sandbox (by
SQL dialect only), the fault operators, the verifier. Tests use a made-up
schema on a real PostgreSQL.

## What this does not do yet

- **Counters and derived columns.** A column the app recomputes (a
  duration from two times, an amount from a rate) has no value rule yet. Its
  bookkeeping fault passes, so its kind is not admitted. A next step is
  enumerating exact expressions over the row's columns (Daikon-style
  templates [A1]) and admitting one only if it holds on every demonstration
  and held-out write and catches its fault.
- **Log tables.** A table whose added rows vary in number (an audit log) is
  never named a log automatically. Those kinds aren't admitted until a rule
  pins such rows to the write (its record or the acting user) and a
  planted extra row is still caught.
- **Excuses on rows other than the main record** (a parent's counter) have
  no value rule and fail their bookkeeping fault.
- **Learned excuses** (`actor_row`) are still approved by a person in the
  prototype. The replacement is in the table above.
- **The identity region** is designed here, not built.

Each gap shows up as an exception, never as a pass.

## Measuring it

On development apps only (the first study's and the first retest's), never
on a pre-registered held-out app or its pack. Per app:

- setup minutes, wall clock, from first command to admitted contracts;
- person decisions (target 0) and exception items;
- kinds admitted;
- planted-fault coverage (stopped / planted, per class);
- the pack's own faults and correct writes run against the admitted
  contracts: false stops per 100 correct writes, faults passed as verified,
  faults caught.

A kind that isn't admitted stops all of its tasks. That is safe but costs
false stops, so coverage (kinds admitted) is reported next to the decision
count. Zero decisions with nothing admitted is not success.

## Safety review

Changes to generator logic (`now_checks`) need a recorded independent
adversarial review before merge. Points for the reviewer:

- `now_checks` only adds a check; it never removes one. A time stamp
  already pinned or unchecked by a person's answer stays as it was unless
  the answer sets `now_checks: true`.
- The window is the execution window plus the recheck plus a minute, in
  either the database's local clock or UTC. A wrong time inside the window
  passes; a NULL fails.
- The sandbox writes only to a store named `*_oa_sandbox` or `oa_sandbox`.
- A kind not admitted has no contract in the production manifest.
