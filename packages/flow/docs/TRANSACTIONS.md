# External transactions: plan, apply, undo

`openadapt plan` gives a batch of agent writes to external systems (a REST
API, a SQL database, email) four properties:

1. **Dry-run diff.** Before anything is sent, you see what would change, for
   example "398 crm.contacts updated, 14 outbox.outbox queued, 1 anomaly:
   large refund: amount = 42000". Approval binds to that exact plan digest and
   can be used once.
2. **Idempotent retry.** Each write carries a key derived from the task, the
   system, the collection, the operation and the record slot. The key never
   comes from the payload. After a crash or a rewind, a retry that regenerates
   a different payload is refused (`replay_mismatch`). An identical retry gets
   the stored result, and nothing is sent.
3. **Verified undo.** Each applied write has a compensating action. After the
   undo, every touched record and the whole frame around it (the collection,
   every table of the database) are read back and compared with the baseline.
   Irreversible writes (email, a delete the API can't reverse) wait in a
   cancelable holdback queue before they are released.
4. **Receipts.** Plan, apply, undo and release each write a `report.json`
   (digests and counts, never record values) and a signed `record/run/v1`
   receipt. The receipt's `approval` block commits to the approved plan
   digest.

Healthy runs make no model calls.

## The proxy: SQL first

The quickest way in is a database you already have:

```
openadapt proxy --postgres "$DATABASE_URL" -- claude
openadapt proxy --sqlite app.db --flag users.role=admin -- claude
openadapt proxy --sqlite app.db        # serve here; keys: y approve, n decline, u undo
```

OpenAdapt holds the credentials. The agent gets a Model Context Protocol
endpoint on `127.0.0.1` with a per-run token and four tools: `sql_query`,
`sql_preview`, `sql_apply` and `sql_undo`. The command starts with
`DATABASE_URL`, the `PG*` variables and any copy of the URL removed from its
environment. For `claude`, the proxy passes `--mcp-config`; any other agent
reads `OPENADAPT_MCP_URL`, `OPENADAPT_MCP_TOKEN` and `OPENADAPT_MCP_CONFIG`.
Each write statement goes through five stages:

1. **Pre-read.** Open a transaction and read every table.
2. **Dry-run diff.** Run the statement, read every table again, and roll
   back. The diff is exact and row-level, for example "users: 412 rows
   changed" and "rows where users.role = admin: 3".
3. **Gate.** A plan that touches at most `--auto-approve-max-rows` rows (0 by
   default) and has no anomaly is approved by policy, and its receipt says
   `agent`. Any other plan waits for a person, who presses `y` in the proxy's
   terminal or runs `openadapt plan approve` in another one. The approval
   binds to the plan digest and is used once.
4. **Execute once.** The statement runs again in a transaction. It commits
   only when the diff read inside that transaction is exactly the planned
   diff: the same rows, the same changed columns and the same values (column
   digests). Added rows are compared without their generated keys.
   Otherwise it rolls back (`diverged`). The dry run is staged twice. A
   column whose value differs between the two runs is *volatile* and is not
   compared: `now()`, a time default, or a timestamp within ten minutes of
   now. A volatile column that isn't a time, such as `random()`, is an
   anomaly, so the plan waits for a person. A plan whose database moved
   since the dry run is refused before anything runs (`plan_stale`).
5. **Read back and undo.** Every table is read again and compared with the
   prediction. The undo images (each changed or removed row's before-image,
   and each added row's key) give a generated undo. The undo runs in one
   transaction, with each row under a savepoint, so a row that a foreign key
   cascade removed is restored after its parent. It is verified when every
   table reads back as it was. Each stage writes a signed receipt.

**Idempotency for raw statements.** A statement's record is the set of rows
it changes or removes, read from the dry run. A regenerated statement after
a restore, such as `balance + 100.00 ... AND active IS NOT NULL` in place of
`balance + 100`, touches the same rows and gets the same key. Because new
work can also touch the same rows, the proxy doesn't refuse it outright. It
marks the plan `possible_replay`, never approves it by policy, and waits for
a person. Any overlap with rows the task already wrote counts, not only the
exact same set: `id IN (9, 10)` after `id = 9` is held too. The default task
is one per database and project folder, stable across proxy restarts. Use
`--task` to start a new one. A statement that only inserts is keyed by its table, with the same
rule. An identical statement after the first apply changes nothing, and its
dry run is empty.

**What the dry run covers.** It covers SQL effects inside the transaction:
rows, triggers that write rows, and cascades. It does not cover effects
outside the database: a trigger that calls out, `NOTIFY` listeners, email
sent by the application, sequences (a rolled-back `nextval` is not returned),
or MySQL DDL (MySQL commits DDL at once, so it is refused). An app that other
clients also write to needs the read-back: the apply-time diff check refuses
a statement whose effect changed, and `plan_stale` refuses a plan whose
database moved, but a write that lands after the commit is not covered. A
schema change (DDL) on SQLite or PostgreSQL is staged and shown, but has no
generated undo.

**The credential boundary.** The agent never receives the credentials in its
environment. An agent running as the same OS user with a shell can still read
credentials the user keeps elsewhere, such as a `.env` file or `~/.pgpass`.
For a hard boundary, keep them where the agent's user can't read them. This
is what the hook pack's `--strict` mode needs ([HOOKS.md](HOOKS.md)): with the
proxy, the agent has no write path to the database except the proxy.

```
openadapt plan create --tx openadapt.tx.yaml --task T-42 writes.json
openadapt plan approve 3f2a9c1e        # a person, at a terminal; types the digest
openadapt plan apply   --tx openadapt.tx.yaml 3f2a9c1e
openadapt plan undo    --tx openadapt.tx.yaml 3f2a9c1e
openadapt plan release --tx openadapt.tx.yaml 3f2a9c1e   # held email, after holdback
openadapt plan cancel  3f2a9c1e                           # drop held writes instead
openadapt plan status
```

## The tx file

`openadapt.tx.yaml` (`openadapt.tx/v1`) declares each system and how to read,
list, write and undo it. Everything specific to an app lives in this file,
never in the engine. See the docstring of
[`openadapt_flow/txplan/spec.py`](../openadapt_flow/txplan/spec.py) for every
key. Three kinds of system:

| Kind | Dry run | Read-back frame | Undo |
|---|---|---|---|
| `sql` (SQLite, MySQL/MariaDB, PostgreSQL) | **Staged transaction.** The planned statements run inside a transaction. Every table is read before and after, inside that transaction, and then the transaction is rolled back. Triggers and cascades appear in the diff. | Every table of the database (`frame_ignore_tables` and `frame_ignore_columns` excluded) | A create is deleted by key. An update gets back its whole before-image, including columns the database set itself. A delete is reinserted with its exact row. |
| `rest` (JSON over HTTP) | **Computed shadow.** The baseline merged with the planned fields. `server_fields` are ignored, and `derived` fields are expected to change with their cause. | The whole collection, through its list route, every page | A create is deleted. An update is restored, and for `replace: true` APIs the full baseline is sent. A delete waits in holdback unless `delete_reversible` is set. |
| `mail` (SMTP and a Mailpit API) | **Holdback.** A send is queued, never sent at apply. | The mailbox | Cancel before release. A released email can't be undone. |

A write is `{"system", "collection", "op", "id", "fields"}`, with `op` one of
`create`, `update`, `delete` or `send`. An update or delete names the
record's `id`. A create or send has no id: its slot is its ordinal within the
task (`create#1`, `create#2`).

## Why the key isn't the payload

A checkpoint restore makes an agent regenerate a request that is slightly
different from the one it already sent: a new reference id, a reworded memo.
A server that dedupes on a request id or on the payload sees a new request,
and the write lands twice. The ACRFence paper calls this a semantic rollback
attack, specifically *action replay* (see [References](#references)).
`openadapt plan` keys each write on what OpenAdapt knows independently of the
model: the task id, the system, the collection, the operation, and the record
id or the create's ordinal. It stores the payload digest and the plan digest
under that key.

- A second write under a key whose payload digest differs is refused before
  anything is sent. The plan already shows it as an anomaly ("replays an
  applied write with a different payload").
- The plan digest is bound to the key, not part of it. If the plan digest
  were part of the key, a re-plan would mint a fresh key and defeat the guard.
- An open attempt, where the send may have landed but the outcome was never
  recorded, is resolved by reading the record back, never by sending again.
  An update that reads back as planned is adopted. A create that can't be
  attributed is held as `UNCERTAIN` for a person.
- The ledger, approvals and holdback queue live in `~/.openadapt/tx` (or
  `$OPENADAPT_TX_STATE`), outside the agent's workspace. A restore of the
  workspace can't roll them back.
- Approvals are single-use. Apply consumes the approval before its first send,
  so a consumed approval can't authorize a second apply. This covers the
  paper's second class, *authority resurrection*.

The task id must be OpenAdapt's own (a run or ticket id), not text the model
writes. A model that can choose a new task id for each retry can mint new
keys.

## Before apply

`apply` refuses, and sends nothing, when:

- the tx file changed since the plan (`spec_changed`);
- any key was already used with a different payload (`replay_mismatch`);
- any frame moved since the plan, which makes the plan stale (`plan_stale`).
  In that case the approval isn't consumed;
- the plan has no unused approval (`not_approved`, `approval_consumed`).

After the writes, apply reads every frame again and compares the diff with
the prediction. A **missed effect** is something the read-back found that the
dry run didn't predict. A **false diff** is something the dry run predicted
that the read-back didn't find. Either one makes the outcome
`RECONCILIATION_REQUIRED`.

## Undo

`undo` runs latest write first, and only on writes this plan applied:

- It refuses a record that changed since apply (`changed_since_apply`). A
  person's later edit is never overwritten.
- Held writes are cancelled.
- After compensating, it reads each record back. A create must be gone. An
  update or delete must equal its baseline.
- It then compares the whole frame with the baseline. Any other change is
  collateral, and then no undo in the plan counts as verified.

Some effects can't be undone exactly: a trigger that writes an audit row on
every update, or an auto-increment counter. When the trigger's rows are in
the frame, the undo is reported `RECONCILIATION_REQUIRED` with the collateral
count, never `ROLLED_BACK`. Counters aren't rows and aren't compared.

## Hooks and wrap

`openadapt plan apply` is the executor: OpenAdapt makes the write. For a
coding agent that also has direct access, declare the same system in
`openadapt.systems.yaml` with `approval: block` ([HOOKS.md](HOOKS.md)), so a
direct write is refused and batch writes go through a plan. `openadapt wrap`
([WRAP.md](WRAP.md)) is a browser MCP harness, not a broker for these
systems. Like the hooks, this layer is advisory against an agent that holds
its own credentials to the system.

## Measured

All runs took place on 21 November 2026, on the lab laptop's WSL with
synthetic data. The rules (server-set and derived fields, ignored tables) were
fixed after a pilot. The pilot found one engine bug: an undo of an SQL update
didn't restore a column the database sets itself
(`ON UPDATE CURRENT_TIMESTAMP`). It was fixed by restoring the whole
before-image.

There were two measured runs. Run 1's receipts weren't signed, because the lab
environment's `openadapt-types` predates `record/run/v1`. Run 2 used the
repository's own types and signed every phase. Its numbers are below. Run 1's
numbers were the same. The results (counts and record ids only) are in
[`benchmark/ext_tx/results/`](../benchmark/ext_tx/results/). The apps run used
code at `2668a20f`, and the proxy and setup runs used `c88d728c`, which also
compares values at apply.

| Surface | Writes | Missed effects | False diffs | Verified undo | Double effects through |
|---|---|---|---|---|---|
| `openadapt proxy` on PostgreSQL 16: 8 statement kinds, including bulk updates, `INSERT ... SELECT` and cascading deletes | 120 statements, 1,490 rows | 0 | 0 | 120 of 120 (every table back to its start, checked by `psql` checksums) | 0 of 20 regenerated statements (all 20 held for a person); the control arm doubled 20 of 20 |
| Vikunja 2.7.1 (REST, computed dry run) | 100 (60 updates, 40 creates) | 0 | 0 | 100 of 100 | 0 of 20 (20 refused); the control arm doubled 20 of 20 |
| Dolibarr 21.0.4 on MariaDB 11.4.13 (SQL, staged dry run, about 400 tables) | 100 (50 updates, 20 deletes, 30 creates) | 0 | 0 | 100 of 100 | 0 of 20 (20 refused); the control arm doubled 20 of 20 |
| Mailpit v1.27.10 (email, holdback) | 100 sends | 0 | 0 | 50 cancelled in holdback and never sent; 50 released exactly once. A released email has no undo. | 0 of 20 (20 refused); the control arm doubled 20 of 20 |

- **Crash after the send, before the outcome was recorded** (5 per app, REST
  and SQL): there was no second effect in 10 of 10 cases.
- **Identical replays** sent nothing in 60 of 60 cases.
- **The independent read-back** (the verifier's `capture_database` and
  `diff_database`) agreed with the proxy's dry run on every table's counts for
  120 of 120 statements.
- **Seeded faults, to show the checks can fail:**
  - A change injected during apply was counted as a missed effect in 10 of 10
    cases.
  - A change injected before undo was caught in 10 of 10 cases. In run 1, one
    of these hit the planned record, and the undo refused to overwrite it
    (`changed_since_apply`).
- **A trigger probe** (PostgreSQL, outside the measured set): the dry run
  showed the trigger's 5 audit rows. The undo fires the trigger again, so it
  reported `RECONCILIATION_REQUIRED` with 5 collateral rows.
- **Receipts:**
  - In the apps run, 660 of 660 phase receipts were signed and verified. Every
    apply, undo and release receipt binds its approval to the plan digest.
  - In the proxy run, 446 of 446 were signed and verified. There, a script
    pressed `y`, so 139 apply receipts say `person`, although no person
    approved them.
- **One session over the real MCP client SDK** (`mcp` 1.x, streamable HTTP)
  listed the four tools, applied a statement and undid it.
- **A live Claude Code session** went through the proxy (Claude Code 2.1.305,
  headless, `--allowedTools` limited to the proxy's tools). The bulk update was
  held: 60 rows, 2 of them admins. Claude Code reported the refusal and didn't
  work around it. After the approval, a second session applied it and undid
  it. See [`results/2026-11-21-live-claude-code.md`](../benchmark/ext_tx/results/2026-11-21-live-claude-code.md).
- **Setup time:** 11.5 to 12.4 s over three runs (run 1: 8.2 to 9.8 s). That is
  from an empty virtual environment to the first diff on a local PostgreSQL:
  installing `openadapt-flow`, `openadapt-types` and `psycopg` with no package
  cache, then `openadapt-flow proxy --postgres ... -- <stand-in agent>` and its
  first `sql_preview`. The target was under 2 minutes. Not included:
  installing Python and uv, and building the wheels from source, which stood
  in for PyPI. The machine was the lab laptop, not a fresh one.
- **Median dry-run time:** 0.87 s for 600 users and 1,500 orders. A statement
  is staged three times.

The kill criteria were a verified undo failing on more than 5% of supported
writes, a dry-run diff missing an effect that the read-back found, a double
effect getting through, and setup over 2 minutes. None was tripped.

## References

- Zheng, Yang, Zhang, Quinn. *ACRFence: Preventing Semantic Rollback Attacks
  in Agent Checkpoint-Restore.* arXiv:2603.20625, March 2026.
  <https://arxiv.org/abs/2603.20625>
- Dong et al. *DeltaBox: Millisecond-Scale Sandbox Checkpoint and Rollback
  for Stateful AI Agents.* arXiv:2605.22781. <https://arxiv.org/abs/2605.22781>
  DeltaState is its OS abstraction, and it rolls back only state inside the
  sandbox.
- Li et al. *PACE: Provenance-Aware Capability Enforcement for Tool-Using LLM
  Agents.* arXiv:2610.01349. <https://arxiv.org/abs/2610.01349>
- Rubrik, *Rubrik Unveils Agent Rewind For When AI Agents Go Awry*, 12 August
  2025.
  <https://s203.q4cdn.com/667520861/files/doc_news/Rubrik-Unveils-Agent-Rewind-For-When-AI-Agents-Go-Awry-2025.pdf>
- Neon, *Branching*. <https://neon.com/docs/introduction/branching>
