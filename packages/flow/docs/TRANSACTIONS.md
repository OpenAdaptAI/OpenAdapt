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

## API and SaaS systems

A hosted API has no frame to diff. For those systems, `openadapt proxy --api`
checks each write by reading the record back and by reading the system's own
event or audit log for other changes by the same credentials. See
[API_VERIFICATION.md](API_VERIFICATION.md).

## Lab results

`benchmark/ext_tx/run.py` runs every flow against real apps on the lab
machine: Vikunja 2.7.1 (REST), Mailpit v1.27.10 (email), and Dolibarr 21.0.4
on MariaDB 11.4.13 (a database-backed app, written through SQL). The results
are in `benchmark/ext_tx/results/`. See [the summary below](#measured).

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
