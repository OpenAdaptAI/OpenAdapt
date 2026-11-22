# Verifying writes to API and SaaS systems

A hosted system such as Epic, Salesforce, Stripe or Linear gives you no
database to diff. OpenAdapt can't prove that nothing else in such a system
changed. What it checks instead is narrower, and it says so in every result:

1. **The record reads back as intended.** After the write, the target record
   is read through the API, normalized with the collection's codecs
   (`null_empty` and the others in `openadapt_flow.tx.judge`) and compared
   with the intended fields. On an update, every other field of the record
   must be unchanged.
2. **The system's own log shows no other change by the same caller.** The
   system's event or audit log is read from a cursor taken just before the
   write. The write's own event must appear, and no other resource may have
   been changed by the same credentials in that window.

Changes by other callers, and changes the log doesn't record, are outside
this scope.

## Where it runs

The path is `openadapt_flow.txplan.scoped.scoped_write`. It serves the
`api_read` and `api_write` tools on the same MCP endpoint as the SQL tools of
`openadapt proxy`:

```
openadapt proxy --api systems.tx.yaml -- claude
openadapt proxy --postgres "$DATABASE_URL" --api systems.tx.yaml -- claude
```

The proxy holds the API credentials. The agent's environment has every
credential named in the tx file removed, along with any copy of it.

## Describing a system

A system is an `openadapt.tx/v1` REST system ([TRANSACTIONS.md](TRANSACTIONS.md))
with an `audit:` block, plus a `scoped:` block on each collection. No code
names an app. Three common log shapes use the same reader:

| Shape | Example | Events | Paging |
|---|---|---|---|
| FHIR `AuditEvent` search | Medplum | `entry.*.resource` | `link[relation=next].url` |
| Event stream | Stripe `/v1/events` | `data` | `starting_after` while `has_more` |
| REST activity feed | Gitea user feed | a bare list | page numbers |

The packs in `benchmark/api_scoped/packs/` are working examples. The
`audit:` block gives:

- the list path and query,
- where the events are in each page,
- where each event's id, server time, actor, resource and action are,
- which server action names mean create, update, delete or read (anything
  else is `unknown`),
- which operations the log records (`logs`),
- how this token's actor is named in the log,
- how long the log may trail the write (`lag`), and
- how far out of request order its server times can be (`skew_s`).

The window is placed by event ids, not by time alone: the cursor keeps the
ids of every event within `skew_s` of the newest one, and the window reads
back that far. Medplum's AuditEvent times were measured up to about 2 seconds
out of request order, so a cursor on time alone dropped the write's own event
in about 1 write in 100. A log page that comes back empty, or a read that
ends before it reaches the cursor, is read again and never taken as "no
events".

A collection's `scoped:` block gives:

- how its records appear in the log (`audit_ref`, for example `Patient/{id}`),
- the fields that identify the record a create makes (`identity`),
- an optional idempotency marker with a search for it, or a conditional-create
  header,
- paths the server sets itself (`volatile`),
- per-field codecs, and
- `logged_fields`, when the log records only some updates (a feed that shows
  a close but not a title edit).

## Idempotency at the tool boundary

Each write has a key derived from the task, the system, the collection, the
operation and the record. For an update, the record is its id. For a create,
it is the normalized value of the collection's `identity` fields, or a `slot`
the caller passes. The key never comes from the payload. It is written to the
ledger (`~/.openadapt/tx`) before the write is sent.

- **The same payload again** returns the first result, and nothing is sent.
- **A regenerated payload** for the same record is refused (`replay_mismatch`),
  and nothing is sent. This covers the ACRFence semantic-rollback attack,
  where an agent restored from a checkpoint writes the same record again with
  different bytes.
- **A crash between the send and the ledger** is recovered by finding the
  write, not by sending it again. The search uses the marker when the
  collection has one, and the log otherwise. A create is sent again only when
  the marker search, or the log over its whole lag budget, shows it never
  landed. Otherwise the result is `COMPLETED_UNVERIFIED`
  (`recovery_unproven`).
- **On the wire**, the key goes out as `Idempotency-Key` (`idempotency_header`)
  where the API honors one, such as Stripe. A FHIR server gets an identifier
  marker and an `If-None-Exist` conditional create.

An update or delete is keyed on its record. A second, deliberate write to the
same record in one task names a new `slot` (`step-2`). Without one, it is
refused as a replay.

The ledger's `begun` event holds the record as it read before the write, so
that a recovery after a crash can still judge the frame. The state folder is
owner-only (mode 0700, files 0600). Put it on encrypted storage when the
records hold personal data.

The guard is only as good as the `identity` fields. If they include a value
that a regenerated payload can change, the retry gets a new key and is sent as
a new record.

## Verdicts

| Result | Codes | Outcome |
|---|---|---|
| Hard failure | `wrong_value`, `partial_write` (an intended field still at its old value), `frame_clobbered` (another field of the record changed), `missing_resource`, `still_present` (a delete), `duplicate_write`, `collateral_mutation` (another resource changed by the same actor) | `FAILED` |
| Logged only | `unknown_events_logged` (an action name the pack doesn't classify) | `VERIFIED` |
| Retried, then not verified | `audit_lag`, `audit_unreadable`, `recovery_unproven` | `COMPLETED_UNVERIFIED` |
| Refused before sending | `replay_mismatch`, `missing_resource` (an update or delete of a record that doesn't exist) | `HALTED` |

A slow read-back and a log that trails the write are retried within
`readback_attempts` and `lag.attempts`. Every outcome writes a `report.json`
and a signed `record/run/v1` receipt in the state folder's `runs/`. The report
holds field paths, codes and counts, never record values. The receipt line is
`confirmed`, `refuted` or `not_checked`, with strength `read_back`.

## What it doesn't check

- Changes made by another caller, and server-side effects the log doesn't
  attribute to this caller, such as a trigger or a background job.
- Changes of a kind the log doesn't record. Gitea's activity feed records
  creates, comments, closes and reopens, but not title or body edits.
- On a create, fields the server adds. Only the intended fields are judged.
- FHIR `_history`. A server without system-level history, such as Medplum,
  shows collateral changes in `AuditEvent` only.
- The log for an update that changes nothing (the record already held the
  intent). Medplum logged most such updates but not all, so none is required
  (`no_change_update`), and a repeat of one can't be seen.
- Where a log names no caller, such as Stripe's Events API, which names the
  request but not the key, every API-caused event in the account is in
  scope. Give the verifier an account or project of its own.
- A collateral write that the log shows only after the write's own event, in
  a log that is written asynchronously, and an event stamped more than
  `skew_s` out of order.

## Results

`benchmark/api_scoped/RESULTS.md` has the measurement on Medplum 5.2.8 and
Gitea 1.24.6, with planted faults and clean writes.
