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

- writes after `read_at` (delayed or queued writes);
- stores the contract doesn't read, by name when known: email, other
  services, files;
- for a contract with no `frame.database`, tables outside the read set.

`result.to_dict()` is the JSON reply for agents and the CLI. It holds closed
fields only: no record values.

## Before the write

`begin_write` raises a subclass of `WriteBlocked` when the write must not be
sent. Adapters turn it into a `written: false` reply.

| Exception | When |
|---|---|
| `BaselineUnreadable` | The store can't be read, so nothing could verify the write afterwards. |
| `WriteHeld` | An earlier attempt with the same identity is in flight in another worker, `UNCERTAIN`, or `REFUTED` and not yet released by a person. |
| `AlreadyWritten` | An earlier attempt was `CONFIRMED` but its reply never reached the caller (a cancellation, a timeout, a crash). It carries the earlier result, and the write isn't sent again. |

## The uncertain-write ledger

The ledger stops a restart or a second worker from resending a write whose
outcome isn't known.

**Identity.** A write's key is an HMAC-SHA256 over the contract's digest, the
bound values, and an optional caller `write_key`. The HMAC key is a secret in
the ledger folder (mode 0600), so the ledger never holds values or plain
digests of them. Two intended writes with the same values need distinct
`write_key`s.

**Storage.** `FileLedger` is the default, in `~/.openadapt/ledger/`
(`OPENADAPT_LEDGER_DIR` overrides it). It keeps one append-only JSON Lines
file per key. Events are `begun`, `resolved`, `delivered` and `released`.
Nothing is rewritten or deleted. A `LedgerBackend` protocol (`events`,
`append`, `lease`) lets a deployment keep it elsewhere, such as a shared
database for workers on several hosts. `MemoryLedger` is for tests.
`ledger=None` turns the ledger off; use it only for a sandbox that resets
between runs, such as RL episodes.

**Leases.** `begin_write` takes an exclusive lease on the key (`flock` on a
lock file) and holds it until `verify_write` returns. The operating system
releases a lease when its process dies. So a second worker that can't get the
lease knows the first is alive and refuses (`WriteHeld`). A worker that gets
the lease while an attempt is still open knows its owner died between the
send and the read-back, and reconciles it before anything else.

**State of a key.** This is decided from its latest attempt:

| Latest attempt | `begin_write` |
|---|---|
| none, `CONFIRMED` and delivered, or `retry_safe` | new attempt |
| open, lease held elsewhere | `WriteHeld` (in flight) |
| open, lease free (owner died) | reconcile, then decide again |
| `CONFIRMED`, not delivered | `AlreadyWritten` with the stored result |
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

The attempt's baseline and values stay in its private folder (mode 0700,
files 0600) next to the ledger, as verify-run keeps baselines today.

**People.** `openadapt verify ledger list` shows open and held keys (no
values). `release KEY --reason TEXT` appends a `released` event after a
person checked the record. `reconcile [KEY]` reads back.

## Modules

| Module | After this change |
|---|---|
| `openadapt_flow.verifier` | The one public API: `Contract`, `Actor`, `Source`, `begin_write`, `verify_write`, `verified_write`, `reconcile`, `WriteResult`, `Outcome`, `Scope`, `PendingWrite`, the `WriteBlocked` family, `VerifierError`, the ledger classes, and #1588's `capture_database`/`diff_database`. A test pins the signatures. |
| `openadapt_flow.verify` (#1589) | A thin, deprecated wrapper with the same signatures. It warns on import. `VerifiedWrite`/`WriteRefused` become one `WriteResult`, and `to_dict()` keeps the old `verdict` key next to `outcome`. Its `BaselineUnreadable` is `WriteBlocked`, so existing `except BaselineUnreadable` blocks also refuse held writes. |
| `openadapt.verify` (launcher) | Re-exports `openadapt_flow.verifier`. |
| `agent/verify_tools.py` (MCP) | Calls `begin_write`/`verify_write`; the ledger applies through them. |
| #1609 `WriteGuard` | Its in-memory `_held` and `_lost_replies` become ledger reads. The owner of #1609 drops them and catches `AlreadyWritten`. |

#1588's `capture(contract, params)` and `verify_write(contract, before, ...)`
had no importers outside these PRs. The replacements are
`begin_write(contract, params, actor=...)` and `verify_write(pending)`. Its
single-use snapshot record (`issued/`) becomes the ledger attempt plus
verify-run's own `.used` marker.

## What this doesn't do

- It doesn't make a write idempotent in the target system. It stops this
  process, a restart, or another worker that shares the ledger from resending
  while the outcome is unknown. A writer that bypasses the verifier isn't
  covered.
- Workers on several hosts need a shared backend. `FileLedger` locks work on
  one machine's local disk, not on network file systems.
- If a crash happens after `verify_write` records `delivered` but before the
  reply reaches the agent, the reply is lost, and an identical call counts as
  a new write. `write_key` closes that gap for callers that have a stable
  task id.
