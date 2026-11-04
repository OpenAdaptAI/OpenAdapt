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

A moved row can be excused for one reason only: it is the acting user's
own row (`actor_row`). Examples: the user's `last_login`, the user's
token's `last_seen`, a property row added for the user.

The acting user's row is the users table's row whose key is the actor's
id, or a row whose user columns hold the actor's id. When a row has more
than one user column holding the actor's id (a token's `user_id` and its
`issued_by`), the excuse names all of them, and each must hold the actor's
id. A token the agent issued for a colleague has the agent in `issued_by`
but the colleague in `user_id`, so it is not the agent's row. You name the users
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

### Collateral on the written row stays a stop

A column that moves on the row the write declares, beyond the fields the
contract lets it change (a modified time, a version, a reassignment), is
never learned. The learner can't tell a column the app sets there from one
the agent's own request sets under another field name: an `assignee` field
in the body that the app stores as `assigned_to` looks the same. So such a
move stays a stop, and a `frame.learned` entry with `relation: target` is
refused when the contract loads.

When the app really does set such a column on its own, a person excuses it
where the contract is made: in the contract generator's bookkeeping answer
([GENERATE_CONTRACTS.md](GENERATE_CONTRACTS.md)), which lists what the write's own row may
change.

## A column the agent sets is never the app's own

Shadow mode watches the agent's own traffic, so an agent that makes the
same mistake in every write (it renames the record, and also reassigns it)
would look like the app's bookkeeping. The learner never proposes an
excuse for a column that:

- any of the agent's requests sent, by name: a field at any depth of a
  write's body, or a query-string parameter of any request through the
  shadow up to the check (`learn collect` records these names);
- a run parameter names; or
- the contract names (its effects' selectors and record fields, and the
  frame's target fields).

Names are compared without case, and a dotted name by its last part.
Such a move stays a stop, and the proposal says why.

When a kind's request bodies weren't recorded (a transport the shadow
can't parse, or checks collected some other way), the learner proposes no
excuse for that kind, and says so.

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
        actor_columns: [id]
      - table: user_property
        columns: [property, user_id, value]
        change: added
        relation: actor_row
        actor_columns: [user_id]
        max_rows: 1
    proposal_sha256: sha256:...
    approved_by: Dana Reviewer
    approved_at: "2026-10-31T12:00:00Z"
```

- `changed`: an existing row must move exactly these columns, the set
  seen moving together.
- `added`: a new row must set exactly these columns (every other column
  stays NULL). An `added` `actor_row` excuse also says `max_rows`: the most
  such rows one learning check saw. More rows in one check are collateral.
- A removal is never excused.
- Never a whole table, a wildcard, or "any row".

## How the verifier enforces it

At every check, and at every recheck, the verifier works out the relation
again from this run:

- An `actor_row` excuse covers a row only when every one of its actor
  columns holds this run's actor id before and after the write. Another
  user's row, a row the actor issued for someone else, a row that moved to
  the actor, or a row added for someone else is still collateral.
- The moved columns must be exactly the excuse's set. Another column of
  the same row, or a subset never seen alone, is still collateral.
- An `added` `actor_row` excuse covers at most `max_rows` rows per check.

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
of every `N` (by default 3 of 5, so 6 of 10, and so on). The count is per
exact column set: two columns that each pass on their own but were seen
together less often never make one excuse. Set them with `--k` and `--n`.

Why 3 of 5: five is the smallest sample in which a majority still allows
two checks where a throttled column didn't move. Some apps write a token's
last-used time at most once a minute. A move seen once or twice in five is
a one-off and isn't proposed. The count is not what keeps the verifier
safe. The relation is: a row that isn't the actor's own is never
proposed, however often it moves.

## Approving

`approve` takes the proposal as written. The proposal's digest catches an
accidental edit (approval is refused: propose again) and records which
proposal an approval came from. It is not keyed, so it doesn't stop
someone who can edit the proposal and recompute the digest; the contracts
themselves are trusted configuration. To leave out an excuse, name it:
`--reject E3`. Approval writes every contract of each kind or none: each
file goes to a temp file first, then all are renamed in. It finds the
contracts from the shadow config's routes
(`--config`), a generator manifest (`--manifest`) or `--contract
KIND=PATH`. Each needs `frame.database`.

Approving again replaces the earlier `frame.learned` with the new one.

## Limits

- A learned excuse is only as narrow as its actor columns. If a table's
  only user column means "created by" rather than "belongs to", an excuse
  on it covers every row the agent created, other users' rows included.
  Read the proposal's actor columns before you approve, and reject an
  excuse whose only actor column is a creator.
- A learned `added` excuse for the actor's row lets the agent itself add
  such a row too (up to `max_rows` per check), with any values in the
  excused columns.
- The actor's id stands for every session of that app user. On a shared
  account, another person's session moves the same rows, and the learned
  columns are excused for them too. Give the agent its own account.
- The rule against columns the agent sets compares names. It is why the
  written row is never learned at all; on the acting user's own rows it
  still lets through a column the agent sets under a field of another name.
  The proposal warns on every excuse: approve only columns the app sets on
  its own, such as times and counters.
- Learning reads SQL mode only. FHIR mode has no database frame.
- Over `frame.database.max_rows` (2,000 rows by default) a table's baseline
  keeps only some columns per row, so a change in another column can't be
  named. Such a row is never learned and never excused: raise `max_rows`
  for an app whose users or tokens table is larger.
