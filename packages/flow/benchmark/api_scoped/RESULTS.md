# API verification on real systems: planted faults and clean writes

This measures resource-scoped verification (`openadapt_flow.txplan.scoped`,
described in [API_VERIFICATION.md](../../docs/API_VERIFICATION.md)) on two
self-hosted systems with synthetic data, run on 21 November 2026:

- **Medplum 5.2.8** (FHIR R4), checked against its `AuditEvent` log. The pack is
  [packs/medplum.tx.yaml](packs/medplum.tx.yaml).
- **Gitea 1.24.6**, checked against its user activity feed. The pack is
  [packs/gitea.tx.yaml](packs/gitea.tx.yaml).

**Stripe was not run.** No Stripe test-mode key was available. The only test
key on hand is a placeholder that the API rejects, and live keys weren't used.
The event-stream reader that a Stripe run would use, [packs/stripe.tx.yaml](packs/stripe.tx.yaml),
is covered only by unit tests against pages in Stripe's shape.

The kill criterion was fixed before the runs: the run fails if it misses any
planted fault that the system's log exposes, or if it stops more than 1% of
clean writes.

## Results

| | Medplum | Gitea |
|---|---|---|
| Clean writes stopped | 0 of 400 | 0 of 400 |
| Clean writes with a receipt | 400 | 400 |
| Seconds per write, median (p95) | 0.19 (0.33) | 0.51 (1.11) |
| Wrong value, create | 10 of 10 stopped (`wrong_value`) | 10 of 10 |
| Wrong value, update or edit | 10 of 10 | 10 of 10 |
| Duplicate create (the executor sends it twice, without the guard) | 10 of 10 (`duplicate_write`) | 10 of 10 |
| Duplicate update (Medplum) or comment (Gitea) | 10 of 10 | 10 of 10 |
| Partial write | 10 of 10, update (`partial_write`) | 10 of 10, create |
| Collateral change, same actor (log exposes it) | 10 of 10 (`collateral_mutation`) | 20 of 20 (a comment on another issue, a close or reopen of another issue) |
| Collateral title edit, same actor (log doesn't record edits) | not applicable | 0 of 10 stopped, as expected |
| Another actor's change in the window (outside the scope) | 0 of 10 stopped | 0 of 10 stopped |
| Same retry, same payload | 10 of 10 returned the first result, nothing sent | 10 of 10 |
| Retry with a regenerated payload (ACRFence) | 10 of 10 refused (`replay_mismatch`), nothing sent | 10 of 10 |
| Kill criterion | not met | not met |

Every planted fault was a real write. The oracle confirmed that the
wrong-value, partial-write and duplicate-create faults landed. Medplum's
oracle reads Postgres directly, and Gitea's reads as a site administrator,
and the verifier never reads through either one. The collateral,
duplicate-update and duplicate-comment faults were checked only by the
server accepting each extra write.

Zero stops in 400 clean writes bounds the false-stop rate at about 0.75%
(95%, rule of three) on each system. On Gitea, 100 of the 400 clean writes
were title edits, which the feed doesn't record. Those were confirmed by
read-back alone (`log_does_not_record_update`). On Medplum, 41 clean updates
set a value the record already held (`no_change_update`), so no log event was
required for them.

The raw rows are in [results/medplum.json](results/medplum.json) and
[results/gitea.json](results/gitea.json). Each row has its outcome, codes,
notes, audit window summary and time. The receipts were written to a
temporary state folder and are not kept.

## What the logs expose

These behaviors were measured by hand before the runs.

| | Medplum `AuditEvent` | Gitea activity feed |
|---|---|---|
| Create | yes | issue and comment: yes |
| Update | yes, including an identical repeated PUT | close and reopen: yes. Title or body edit: no. Label create: no |
| Read | yes (the verifier's own reads, classified and ignored) | no |
| Names the caller | yes (`ClientApplication/<id>`) | yes (login) |
| System-wide history | no (`/_history` returns 400) | not applicable |

## Development runs

The first full runs used a cursor placed on server time alone, and each
system had stops:

- **Medplum** stopped 4 of 300 clean writes (1.3%), which meets the kill
  criterion. In two traces of those stops, the write's own `AuditEvent` was in
  the log, but it was stamped about 1.5 seconds earlier than the newest event
  read at the cursor, which was a read made before the write. The cause inside
  Medplum was not identified.
- **Gitea** stopped 1 of 300 clean writes and 2 of the 10 other-actor
  controls. The cause was not reproduced: 40 comments made in a separate probe
  all appeared in the feed within a second. The fix below removed these stops
  in the final run, but nothing shows it was the same cause.
- **The Medplum partial-write plant** was not stopped in 1 of 10 runs, which
  also meets the kill criterion. That plant set `gender` to `other`, and a
  likely cause is a seed patient whose gender was already `other`, which would
  make the unwritten field correct. This was not confirmed from the data. The
  plant now always picks a gender different from the record's, and the
  oracle checks that the field was left unwritten.

The fix places the window by event ids. The cursor keeps the ids of every
event within `skew_s` of the newest one, and the window reads back that far.
The same runs showed two more fail-open cases, now fixed:

- An `AuditEvent` search that came back empty for a moment placed the cursor
  at the start of the log.
- A window read that ended before it reached the cursor counted as complete.

Both are now read again, and if they persist the result is
`audit_unreadable`. They are tested in `tests/test_txplan_scoped.py`. The
summaries of those runs are in [results/development/](results/development/).

## Limits

- Two systems, one machine (the zenbook lab, Docker in WSL), and synthetic
  records. Each fault was planted 10 times, so a detection rate of 10 of 10
  bounds the miss rate only at about 26% (95%) per fault class. The kill
  criterion is about any miss, not the rate.
- The planted faults are the executor's: wrong values, retry storms without
  the guard, partial bodies, and side writes with the same credentials.
  Server-side effects such as triggers and background jobs weren't planted.
- The idempotency guard depends on the declared `identity` fields. In a
  development run, Medplum's identity included `birthDate`, so a regenerated
  payload with a different birth date got a new key and was not refused. The
  pack now uses `name` only.
- FHIR `_history` is not used. Medplum serves no system-level history, so
  collateral changes are read from `AuditEvent`. A per-resource version count
  (`Patient/{id}/_history`) would be a second signal for duplicate updates,
  but it isn't implemented.
- On Medplum, an update that changes nothing reads the log once, with no
  settle wait (`settle_s` is 0 in the pack). A collateral change that lands in
  the log late, next to such an update, can be missed.
- Stripe: not run (see above).

## Reproduce

The containers run on any Docker host. The bench can run on the same host,
or on another machine with the ports forwarded (`API_SCOPED_SSH`, see
`lab.py`):

```
python benchmark/api_scoped/lab.py up medplum      # ports 9693-9694, loopback
python benchmark/api_scoped/lab.py up gitea        # port 9692, loopback
python benchmark/api_scoped/run.py --system medplum --clean 400 --faults 10 --out benchmark/api_scoped/results/medplum.json
python benchmark/api_scoped/run.py --system gitea --clean 400 --faults 10 --out benchmark/api_scoped/results/gitea.json
python benchmark/api_scoped/lab.py down medplum && python benchmark/api_scoped/lab.py down gitea
```
