# Verifier API

One public module checks one write against the system of record:
`openadapt_flow.verifier`. It replaces the two overlapping APIs proposed in
#1588 (`openadapt_flow.verifier`) and #1589 (`openadapt_flow.verify`). Every
adapter, the MCP tools and the `openadapt verify` CLI call it. It wraps
`verify_run` and checks exactly what verify-run checks.

## The call

```python
from openadapt_flow.verifier import Actor, Contract, begin_write, verify_write

contract = Contract.load("verify.yaml")          # openadapt.verify/v1
pending = begin_write(contract, params, actor=Actor("agent", "intake-bot"))
...                                              # the caller sends the write
result = verify_write(pending)
if result.outcome is Outcome.CONFIRMED:
    print(result.receipt_sha256)
else:
    print(result.outcome, result.reasons)        # a person checks; don't resend
```

`begin_write` does three things, in this order:

1. Consults the uncertain-write ledger for this write's identity. If an
   earlier attempt is still open or unresolved, it raises (see
   [Before the write](#before-the-write)) and nothing is sent.
2. Reads the system of record (the baseline). If the store can't be read, it
   raises `BaselineUnreadable` and nothing is sent.
3. Records the attempt in the ledger, then returns a `PendingWrite`.

`verify_write` reads the store again, judges the write against the baseline,
and records the outcome in the ledger. It never retries or resends the write.

`verified_write(...)` is the same pair as a context manager.

## One outcome vocabulary

`result.outcome` is one of three values. They answer one question: does the
system of record show the declared write, and only it?

| Outcome | Meaning | Resend? |
|---|---|---|
| `CONFIRMED` | Every declared effect was read back from the system of record at the contract's minimum strength. Nothing else in the read set moved, except what the frame excuses. The source check held or wasn't asked for. | Not needed |
| `REFUTED` | The store shows something other than the declared write: a wrong value, another record, a duplicate, or a change outside the declared write. | Never. A person checks. |
| `UNCERTAIN` | The store doesn't show the write, couldn't be read, or was read too weakly to certify it. The write may still land. | Not until reconciliation resolves it |

`result.verified` is `outcome is Outcome.CONFIRMED`. `result.retry_safe` is
true in one case only: the caller said the write was never sent and the store
shows nothing landed (verify-run's `halted_before_effect`).

A caller that can't tell whether the write was sent, such as a tool that
raised before or while acting, leaves `write_attempted=True`. A store that
shows nothing is then `UNCERTAIN`, never `retry_safe`.

**Delivery is the caller's call.** `verify_write(pending, delivered=False)`
records the outcome but marks it undelivered, for example when a tool call
was cancelled or timed out after the read-back. The next identical call
raises `AlreadyWritten` with that result instead of sending again.

**Who declared each value.** `begin_write(..., declared_by=...)` takes one
name (`"agent"`) or a map from value name to who set it (`{"clinic":
"operator", "note": "agent"}`). `to_dict()` returns it as
`values_declared_by`. To bind it into the receipt as well, put it in
`Actor.build`, which verify-run digests.

### Mapping from the old names

| Source | Old name | New outcome |
|---|---|---|
| verify-run receipt `outcome` | `verified` | `CONFIRMED` |
| | `halted_before_effect` | `REFUTED`, `retry_safe=True` |
| | `rejected_policy` (self-approved) | `REFUTED` |
| | `reconciliation_required` | `REFUTED` or `UNCERTAIN` (rule below) |
| | `failed_platform` | `UNCERTAIN` |
| #1588 `WriteResult.status` | `verified` / `refuted` / `unconfirmed` | `CONFIRMED` / `REFUTED` / `UNCERTAIN` |
| #1589 `verdict` | `VERIFIED` / `REFUTED` / `RECONCILIATION_REQUIRED` | `CONFIRMED` / `REFUTED` / `UNCERTAIN` |
| #1609 adapter reply | `NOT_WRITTEN` | No outcome: the write was blocked before it was sent (`written: false`) |

`reconciliation_required` splits with #1588's rule. It's `REFUTED` when the
frame or the source check is refuted, or when a failed effect was observed
`present` or `conflicting`. It's `UNCERTAIN` when every failed effect was
observed `absent`, or a part wasn't checked. One behavior change from #1589:
a write that lands on another patient now comes back `REFUTED` when the frame
sees the other record move. #1589 called it `RECONCILIATION_REQUIRED`. Both
outcomes block a resend.

The receipt keeps verify-run's `outcome` unchanged. `result.receipt_outcome`
carries it, and the per-part `checked` map (`delta`, `frame`, `source`: each
`held`, `refuted` or `not_checked`) stays as it is.

## What a result establishes

Every result carries `result.scope`, so a reader can tell what was and wasn't
checked:

```python
@dataclass(frozen=True)
class Scope:
    read_paths: tuple[str, ...]   # "database", "api", "fhir", "audit_log", "screen", "file"
    stores: tuple[str, ...]       # each store read, e.g. "sql", "rest", "frame.database", "audit"
    window: Window                # baseline_read_at, read_at, max_execution_window_s
    strength: str                 # "independent_system_of_record" | "independent_session" | "unchecked"
    not_covered: tuple[str, ...]  # what this result says nothing about
```

The read path comes from each verifier's substrate: `sql` is `database`;
`rest` and `graphql` are `api`; `fhir` is `fhir`; the frame's audit read is
`audit_log`; `onscreen`, `ui_readback` and `fresh_session` are `screen`.

`not_covered` always lists:

- writes after `read_at` (delayed or queued writes). When the contract
  sets `frame.database.recheck_after_s` and the result is `CONFIRMED`, this
  line names the recheck's due time instead: the result is provisional
  until the recheck runs;
- stores the contract doesn't read, by name when known: email, other
  services, files;
- for a contract with no `frame.database`, tables outside the read set.

`result.to_dict()` is the JSON reply for agents and the CLI. It holds closed
fields only: no record values.

## Writes that land after the result

With `frame.database.recheck_after_s`, a `CONFIRMED` result has
`recheck_due_at` (also in the receipt), and `result.recheck` holds the private
state the recheck compares with. `verify_write` saves it as `recheck.json` in
the write's folder, beside `baseline.json` and `effect-receipt.json` (mode
0600, keyed hashes only), and records it as issued under the verifier's state
directory.

```python
late = recheck(contract, result, params=params, actor=actor,
               excused_by=[later_result, ...])   # later writes, any outcome
late.outcome   # CONFIRMED, REFUTED (late) or UNCERTAIN
```

The caller can schedule it from `recheck_due_at` and keep working: before
then, `recheck` raises `RecheckNotDue` (`wait=True` sleeps instead). It reads
the whole database again and judges every change since the post-save read.
A later write passed in `excused_by` accounts only for what changed in its
own window. A change that may be what refuted that later write gives
`UNCERTAIN`. See [VERIFY_RUN.md](VERIFY_RUN.md#writes-that-land-later).

## Before the write

`begin_write` raises a subclass of `WriteBlocked` when the write must not be
sent. Adapters turn it into a `written: false` reply.

| Exception | When |
|---|---|
| `BaselineUnreadable` | The store can't be read, so nothing could verify the write afterwards. |
| `WriteHeld` | An earlier attempt with the same identity is in flight in another worker, `UNCERTAIN`, or `REFUTED` and not yet released by a person. |
| `AlreadyWritten` | An earlier attempt was `CONFIRMED` but its reply never reached the caller (a cancellation, a timeout, a crash). It carries the earlier result, and the write isn't sent again. |

It raises `VerifierError` when a value the contract needs is missing from
`params`, or is `None` where a null can't be bound (see below).

### Null values

A parameter set to `None` means the field is left empty. In an effect's
`match`, `new_records` or `value` it binds to a null value, which matches
only a field that is present in the record and null: a SQL `NULL`, a JSON
`null`, or a FHIR path that resolves to nothing.

A missing field is not null. A record without the field matches nothing,
so a misspelled, nested or unprojected field can't pass for null. When the
store leaves a field out of a record whose value is null, declare it on
that field. Redmine's issue JSON leaves out `assigned_to` when no one is
assigned:

```yaml
match:
  subject: {param: subject}
  assigned_to: {param: assignee, absent_is_null: true}
```

The record check and the frame read the same declaration, and it is part
of the contract hash. It is accepted only on a `rest` or `graphql` read
and only on a top-level key: a SQL row or a FHIR record always carries the
field, and the REST reader never resolves a dotted key, so there a
declaration would let any value pass. At least one record of the read must
carry the declared key; if none does, the key is likely misspelled and the
check stops (UNCERTAIN).

A null write and a `""` write are different writes in the ledger. Ledgers
written before keyed `None` as `""`, so a null write also looks at that
old key. While an attempt stands there (in flight, unresolved, refuted, or
a `""` write just made), the null write is held. `release KEY --found
landed` hands that attempt's result back to the null write, and it is not
sent; `--found absent` lets it be sent.

A null value never matches `""`, the text `"None"`, or any other stored
value, so a wrong write on a task with empty fields still stops. The
reverse holds too: a stored null matches no text parameter, `""` included,
and `field_equals` with `""` no longer passes on a stored `NULL`. `""` and
`NULL` are different values, and the write ledger keeps them apart too.
When an app stores an empty field as either one, say so per column in the
query, for example `NULLIF(description, '') AS description`, and pass
`None`.

A read that loses information can confirm a null it shouldn't. A FHIR
field path that never resolves (a typo such as `telecom[system=e-mail]`)
reads every record as null. So does a column from an outer join that
filters its rows (`LEFT JOIN users u ON ... AND u.type = 'User'`), for a
record whose row the join drops. Match a null only on a base column, or on
a FHIR path that resolves when the value is there.

Anywhere else a null has nothing to send or compare: a query, path or
search parameter, an idempotency or composite key, a read-back field, the
frame, or a read of the screen (`ui_readback`, `desktop_readback`), where
an empty field reads as text. `begin_write` refuses a `None` there with a
`VerifierError` that names the parameter, before anything is read.

## The uncertain-write ledger

The ledger stops a restart or a second worker from resending a write whose
outcome isn't known.

**Identity.** A write's key is an HMAC-SHA256 over three things:

- the contract's identity (`Contract.identity`): each effect's kind, target
  and values, unbound, and the store (`Contract.store`: the verifier's kind
  and location and `environment_id`, without credentials; a loopback host
  is one host);
- the bound values, normalized so that `7` and `"7"` are the same write;
- an optional caller `write_key`.

Timeouts, the window and the frame are left out of the identity, so editing
them between a crash and a restart can't make the same write look new. An
address is spelled one way:

- scheme and host lower case;
- a loopback host as `loopback`;
- no default port;
- no trailing slash.

Environment variable names (`*_env`, for example the token that picks a
tenant) stay in the identity; secret values don't. The same write to another
store is its own write. But the same effects and values might also be
recorded under another address for the same store: the same verifier kind,
`environment_id` and host, with the address split or spelled differently.
If that other attempt isn't free (it's in flight, unresolved, or already
made), `begin_write` holds the write: a hold beats a resend. Give each
store its own `environment_id`, so two stores on one host never hold each
other.

If the store's address is edited between a crash and the restart, the old
baseline was read by a different verifier and can't be judged. The attempt
stays held until a person checks the record.

The HMAC key is a secret in the ledger folder (mode 0600), so the ledger
never holds values or plain digests of them. A landed write without a
`write_key` is handed back to an identical call for `repeat_s` (default 10
minutes); after that, the same values are a new write. Inside that window,
two intended writes with the same values need distinct `write_key`s. With a
`write_key`, a landed write is never made again under it. The MCP tool
`verify_begin` takes an optional `write_key`.

**Storage.** `FileLedger` is the default, in `~/.openadapt/ledger/`
(`OPENADAPT_LEDGER_DIR` overrides it). It keeps one append-only JSON Lines
file per key. Events are `begun`, `resolved`, `delivered` and `released`.
Nothing is rewritten or deleted. A `LedgerBackend` protocol (`events`,
`append`, `lease`) lets a deployment keep it elsewhere, such as a shared
database for workers on several hosts. `MemoryLedger` is for tests.
`ledger=None` turns the ledger off; use it only for a sandbox that resets
between runs, such as RL episodes.

**Leases.** `begin_write` takes an exclusive lease on the key, without
waiting (`flock` with `LOCK_NB` on the lease's own open file), and holds it
until `verify_write` returns. Whichever thread runs `verify_write` releases
it, and a second `begin_write` in the same process is refused, never
blocked. The operating system
releases a lease when its process dies. So a second worker that can't get the
lease knows the first is alive and refuses (`WriteHeld`). A worker that gets
the lease while an attempt is still open knows its owner died between the
send and the read-back, and reconciles it before anything else.

**State of a key.** This is decided from its latest attempt:

| Latest attempt | `begin_write` |
|---|---|
| none, `retry_safe`, or landed and delivered more than `repeat_s` ago (no `write_key`) | new attempt |
| open, lease held elsewhere | `WriteHeld` (in flight) |
| open, lease free (owner died) | reconcile, then decide again |
| landed (`CONFIRMED`, or `landed`): undelivered, inside `repeat_s`, or under a `write_key` | `AlreadyWritten` with the stored result |
| `UNCERTAIN` | reconcile, then decide again |
| `REFUTED` | `WriteHeld` until `release` |

**Reconciliation.** `reconcile(contract, key)` resolves an attempt by reading
back. Callers include `begin_write`, `openadapt verify ledger reconcile`, and
an operator's job.

- It judges the attempt against the attempt's own baseline, so a record that
  was there before is never taken for this write. If the process died between
  the send and the read-back, that baseline is unused. If an `UNCERTAIN`
  check already used it, reconciliation reads a private copy. That's still
  the same write, and the lease stops any other attempt at it from being
  sent in the meantime. A copy carries `remediates` with the earlier
  receipt's digest.
- `CONFIRMED` and `REFUTED` resolve the attempt. A `CONFIRMED` reconciliation
  is handed back once (`AlreadyWritten`) instead of a resend.
- `UNCERTAIN` keeps the attempt held. Past the contract's
  `max_execution_window_s`, the baseline proves nothing, so the attempt stays
  held. A held attempt is released by a person. An attempt the store still
  shows `absent` is also released once the ledger's `settle_s` has passed.
  `settle_s` defaults to off, because a queued write can land late.
- An attempt whose process died before it sent anything looks the same as
  one that died just after: it stays held until a person, or `settle_s`,
  releases it.

**Other writes in the window.** `verify_write` and reconciliation both check
the result against attempts of other keys that were open during its window,
from just before its baseline read to the end of its read-back.

- **Same values.** One record can't prove two writes. If another attempt
  with the same values was open during the window, a `CONFIRMED` result
  becomes `UNCERTAIN` and is held for a person. That other attempt may have
  written the record, or this attempt's send may have stored nothing.
- **Added records from other writes.** A correct write can see records that
  other workers added in the same window. The excuse applies only when all
  of these hold:
  - its declared records landed exactly (`delta` held);
  - the only frame breach is added records;
  - there are no more of them than other attempts on the same store wrote
    in the window, counting attempts read back as landed or still holding
    a live lease.

  Then the result is `UNCERTAIN` with `landed: true` and `review: true`. It
  isn't confirmed, and it's never sent again. It stays listed in `openadapt
  verify ledger list` until a person releases it, since a count can't name
  the records. While it's under review, an identical call is handed back
  even after `repeat_s`, so no later attempt hides it. An abandoned attempt,
  or one on another store, never excuses anything, and a breach nothing
  explains stays `REFUTED`.
- **Which open attempts count.** An attempt still open counts while its
  owner holds the lease, or for the contract's window after it began, since
  a write it sent before its owner died may still land. Once a person
  releases an attempt, it's closed. Two concurrent tasks with the same
  values and different write keys both end `UNCERTAIN` and wait for a
  person. That's the conservative call.
- **Lease checks.** Checking whether another key's owner is alive takes
  that key's lease for an instant. A `begin_write` for that key at the same
  moment is refused as in flight. The refusal is safe, but the caller sees
  it.

**What's kept on disk.** While an attempt may still need a read-back, its
baseline and values stay in its private folder (mode 0700, files 0600) next
to the ledger, as verify-run keeps baselines today. Once it resolves for
good (landed, refuted, retry-safe or released), they're removed. Receipts
stay; they hold no record values.

**People.** `openadapt verify ledger list` shows open and held keys (no
values). `release KEY --by NAME --reason TEXT --found absent|landed` records what a person found after they checked the record. `absent` frees the key, so the write may be sent again. `landed` resolves the attempt as landed, so it's handed back and never sent again. `reconcile [KEY]` reads back.

## Modules

| Module | After this change |
|---|---|
| `openadapt_flow.verifier` | The one public API: `Contract`, `Actor`, `Source`, `begin_write`, `verify_write`, `verified_write`, `reconcile`, `WriteResult`, `Outcome`, `Scope`, `PendingWrite`, the `WriteBlocked` family, `VerifierError`, the ledger classes, #1588's `capture_database`/`diff_database`, and its deprecated `capture`/`Snapshot`. Tests pin the signatures. |
| `openadapt_flow.verify` (#1589) | A thin, deprecated wrapper with the same signatures. It warns on import. Results are one `WriteResult`, but `isinstance(r, VerifiedWrite)` still means `CONFIRMED`. `to_dict()` keeps `verdict`, `seal`, `seal_path`, `values_declared_by` and `authorization`. Its `BaselineUnreadable` is `WriteBlocked`, so existing `except BaselineUnreadable` blocks also refuse held writes. The message says which refusal it was; an `AlreadyWritten` carries the earlier `.result`. |
| `openadapt.verify` (launcher) | Re-exports `openadapt_flow.verifier`. |
| `agent/verify_tools.py` (MCP) | Calls `begin_write`/`verify_write`; the ledger applies through them. |
| #1609 `WriteGuard` | Its in-memory `_held` and `_lost_replies` become ledger reads. The owner of #1609 drops them and catches `AlreadyWritten`. |

#1588's `capture(contract, params)` and `verify_write(contract, before,
params=..., actor=...)` shipped in 2.4.1, so they keep working, deprecated:
`capture` warns, and a snapshot still verifies once, only under the state
directory it was captured in (`OPENADAPT_VERIFIER_STATE_DIR`). The result is
the one `WriteResult`: `status` gives 2.4.1's `verified`, `refuted` or
`unconfirmed`, and 2.4.1's `outcome` string (verify-run's outcome) is now
`receipt_outcome`, since `outcome` is `CONFIRMED`, `REFUTED` or `UNCERTAIN`.
A snapshot doesn't go through the ledger, so move to
`begin_write(contract, params, actor=...)` and `verify_write(pending)`.

## What this doesn't do

- It doesn't make a write idempotent in the target system. It stops this
  process, a restart, or another worker that shares the ledger from resending
  while the outcome is unknown. A writer that bypasses the verifier isn't
  covered.
- A store address respelled by hand between a write and its retry (a host
  alias such as `127.1`, a hostname for an IP, a DNS alias) isn't
  recognized as the same store, and the write could be sent again. Keep
  store addresses stable while writes are open, and give each store its
  own `environment_id`. Tenants told apart only by URL path also need
  distinct `environment_id`s. Without them, one tenant's unresolved write
  holds the same write for the other.
- Workers on several hosts need a shared backend. `FileLedger` locks work on
  one machine's local disk, not on network file systems.
- If a process crashes after `verify_write` records a delivered outcome but
  before the reply reaches the agent, the reply is lost, and an identical
  call without a `write_key` counts as a new write. A cancellation the
  caller sees isn't affected (`delivered=False`). `write_key` closes the
  remaining gap for callers that have a stable task id.
