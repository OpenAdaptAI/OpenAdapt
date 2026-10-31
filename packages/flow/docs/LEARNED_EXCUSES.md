# Learned excuses: learn, then enforce

Many apps write to their own database on every request. A sign-in sets the
user's `last_login`. An API call sets the token's `last_seen`. The first
call after a reset adds a row for the user's last login time. The
whole-database frame (`frame.database`) sees these rows move during a
write. Nothing in the contract explains them, so it refutes every write,
correct or not.

`openadapt-flow learn` fixes this in three steps:

1. **Collect.** Run shadow mode in SQL mode (`docs/SHADOW_MODE.md`). Each
   time the verifier checks a write, the learner records which rows moved
   outside the write, by table and column name only.
2. **Propose.** The learner writes a proposal: the moves a person may
   excuse, the moves that stay a stop, and the checks it ignored.
3. **Approve.** One command by a person writes the excuses into the
   contracts as `frame.learned`, with the proposal's digest and the
   approver's name. From then on the verifier excuses exactly those moves.

```bash
openadapt-flow learn collect --config shadow.yaml --actor 2 --out learn/
openadapt-flow learn propose --episodes learn/episodes.jsonl \
    --users app_user --read read.yaml --out learn/proposal.yaml
openadapt-flow learn approve learn/proposal.yaml --by "Dana Reviewer" \
    --config shadow.yaml
```

`python -m openadapt_flow.learn` runs the same commands.

## What can be excused

A moved row can be excused only for one of two reasons:

| Relation | The row is | Example |
| --- | --- | --- |
| `target` | a row the write declares or adds | the record's `modified_at` |
| `actor_row` | the acting user's own row | the user's `last_login`, the user's token's `last_seen`, a property row added for the user |

The acting user's row is the users table's row whose key is the actor's
id, or a row whose user column holds the actor's id. You name the users
table (`--users TABLE` or `--users TABLE.KEY`). The user columns of other
tables come from the schema's foreign keys to that key (`--schema
schema.json` or `--read read.yaml`). You can confirm more with
`--user-column TABLE.COLUMN`. Without a schema, only the columns you name
count.

The actor's id is the `Actor` id the verifier is called with: `--actor` in
shadow mode, `Actor("agent", "2")` from Python. Set it to the agent's user
id in the app.

Any other row can't be excused. It stays a stop, even when it moves in
every check. The proposal lists it under "Stays a stop" so you can see it.

## What an excuse says

Each excuse names one table, the exact columns that moved, the kind of
change and the relation:

```yaml
frame:
  database: {...}
  learned:
    excuses:
      - table: app_user
        columns: [last_login]
        change: changed
        relation: actor_row
        actor_column: id
      - table: user_property
        columns: [property, user_id, value]
        change: added
        relation: actor_row
        actor_column: user_id
    proposal_sha256: sha256:...
    approved_by: Dana Reviewer
    approved_at: "2026-10-31T12:00:00Z"
```

- `changed`: an existing row may move in these columns and no others.
- `added`: a new row may set these columns and no others (any other column
  stays NULL).
- A removal is never excused.
- Never a whole table, a wildcard, or "any row".

## How the verifier enforces it

At every check, and at every recheck, the verifier works out the relation
again from this run:

- A `target` excuse covers only rows this write declares. A look-alike row
  in the same table is still collateral.
- An `actor_row` excuse covers a row only when its actor column holds this
  run's actor id before and after the write. Another user's row, a row
  that moved to the actor, or a row added for someone else is still
  collateral.
- The moved columns must all be in the excuse. Another column of the same
  row is still collateral.

Anything outside a learned excuse is judged as before. The receipt's frame
evidence names the excuses used (`learned.used`) and the proposal's
digest. A contract without `frame.learned` keeps the digests it had.
`frame.learned` needs `frame.database`.

## Which checks teach

A check teaches only when the write's declared effects held and the frame
was refuted by database rows alone. The learner ignores every other check
and counts it by reason:

- confirmed (nothing to excuse);
- the declared effects didn't hold (a wrong or missing write);
- the frame was not checked;
- the frame was refuted by an audit entry, a table no row comparison
  explains, or a missing side record;
- too many moved rows to list (more than 50).

## How often a move must be seen

An excuse is proposed for a kind of write only after at least `N`
learnable checks of that kind, and only when it was seen in at least `K`
of every `N` (by default 3 of 5, so 6 of 10, and so on). Each column of an
excuse must meet the same bar; a column seen less often is left out and
listed. Set them with `--k` and `--n`.

Why 3 of 5: five is the smallest sample in which a majority still allows
two checks where a throttled column didn't move. Some apps write a token's
last-used time at most once a minute. A move seen once or twice in five is
a one-off and isn't proposed. The count is not what keeps the verifier
safe. The relation is: a row that isn't the write's own or the actor's is
never proposed, however often it moves.

## Approving

`approve` takes the proposal as written. If anyone edited it, its digest no
longer matches and approval is refused: propose again. To leave out an
excuse, name it: `--reject E3`. Approval writes every contract of each
kind or none. It finds the contracts from the shadow config's routes
(`--config`), a generator manifest (`--manifest`) or `--contract
KIND=PATH`. Each needs `frame.database`.

Approving again replaces the earlier `frame.learned` with the new one.

## Limits

- A learned excuse is only as narrow as the actor column. If a table's
  user column means "created by" rather than "belongs to", an excuse on it
  covers every row the agent created. Read the proposal's actor columns
  before you approve.
- A learned `added` excuse for the actor's row lets the agent itself add
  such a row too, with any values in the excused columns.
- `target` excuses apply to tables the whole-database frame compares. A
  contract that reads the target table through its own surface (and skips
  it in the database frame) already lists what the target may change in
  `target_fields`.
- Learning reads SQL mode only. FHIR mode has no database frame.
- Over `frame.database.max_rows` (2,000 rows by default) a table's baseline
  keeps only some columns per row, so a change in another column can't be
  named. Such a row is never learned and never excused: raise `max_rows`
  for an app whose users or tokens table is larger.
