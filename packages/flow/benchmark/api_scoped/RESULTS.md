# API verification on real systems: planted faults and clean writes

This measures resource-scoped verification (`openadapt_flow.txplan.scoped`,
described in [API_VERIFICATION.md](../../docs/API_VERIFICATION.md)) on two
self-hosted systems with synthetic data. It was rerun on 22 November 2026
with 50 plants per fault class, after #2437's fail-open fixes merged:

- **Medplum 5.2.8** (FHIR R4), checked against its `AuditEvent` log. The pack is
  [packs/medplum.tx.yaml](packs/medplum.tx.yaml).
- **Gitea 1.24.6**, checked against its user activity feed. The pack is
  [packs/gitea.tx.yaml](packs/gitea.tx.yaml).

**Stripe was not run.** No Stripe test-mode key was available, and live keys
are never used. The event-stream reader that a Stripe run would use,
[packs/stripe.tx.yaml](packs/stripe.tx.yaml), is covered only by unit tests
against pages in Stripe's shape.

The kill criterion was fixed before the runs: the run fails if it misses any
planted fault that the system's log exposes, or if it stops more than 1% of
clean writes.

## Results

| | Medplum | Gitea |
|---|---|---|
| Clean writes stopped | 0 of 400 | 0 of 400 |
| Clean writes with a receipt | 400 | 400 |
| Seconds per write, median (p95) | 0.06 (0.07) | 0.07 (0.59) |
| Wrong value, create | 50 of 50 stopped (`wrong_value`) | 50 of 50 |
| Wrong value, update or edit | 50 of 50 | 50 of 50 |
| Duplicate create (the executor sends it twice, without the guard) | 50 of 50 (`duplicate_write`) | 50 of 50 |
| Duplicate update (Medplum) or comment (Gitea) | 0 of 50 landed: the server refused every second PUT (`If-Match`, 412) | 50 of 50 |
| Partial write | 50 of 50, update (`partial_write`) | 50 of 50, create |
| Collateral change, same actor (log exposes it) | 50 of 50 (`collateral_mutation`) | 100 of 100 (a comment on another issue, a close or reopen of another issue) |
| **Missed:** collateral title edit, same actor (the feed doesn't record edits) | not applicable | **0 of 50 stopped** |
| **Missed:** another actor's change in the window (outside the scope) | **0 of 50 stopped** | **0 of 50 stopped** |
| Same retry, same payload | 50 of 50 returned the first result, nothing sent | 50 of 50 |
| Retry with a regenerated payload (ACRFence) | 50 of 50 refused (`replay_mismatch`), nothing sent | 50 of 50 |
| Kill criterion | not met | not met |

The two **missed** rows are known misses, not failures of the kill criterion.
The check is scoped to the resource and the credentials it holds: a change
the system's log doesn't record (a Gitea title edit), and a change made by
another caller, are outside what it can see. Every one of those plants
landed: the server accepted each side write.

Every planted fault was a real write. A plant counts as a detection only when
it landed; none failed to land in this run. The oracle confirmed that the
wrong-value, partial-write and duplicate-create faults landed. Medplum's
oracle reads Postgres directly, and Gitea's reads as a site administrator,
and the verifier never reads through either one. The collateral,
duplicate-update and duplicate-comment faults were checked by the server
accepting each extra write.

Zero stops in 400 clean writes bounds the false-stop rate at about 0.75%
(95%, rule of three) on each system. 50 of 50 per exposed fault class bounds
the miss rate at about 6% (95%) per class. On Gitea, 100 of the 400 clean
writes were title edits, which the feed doesn't record. Those were confirmed
by read-back alone (`log_does_not_record_update`). On Medplum, 41 clean
updates set a value the record already held (`no_change_update`), so no log
event was required for them.

**Rerun on 22 November 2026 with enforced preconditions (#2529).** Since
#2529, a whole-record update or a delete is `VERIFIED` only on a route whose
`If-Match` precondition has enforcement evidence; without it, the 200 clean
Medplum updates would end `COMPLETED_UNVERIFIED`. Medplum 5.2.8 enforces
`If-Match`: a PUT or DELETE with a version that doesn't match gets 412 and
changes nothing ([probe_precondition.py](probe_precondition.py), output in
[results/medplum_precondition_probe.json](results/medplum_precondition_probe.json)).
The pack now sends the read's `ETag` as `If-Match` on update and delete and
records that probe as its evidence. The Medplum column above is from the
rerun at commit `80c3da70`: 400 of 400 clean writes `VERIFIED`, and every
exposed fault class stopped as before, with one change. In the duplicate
update plant, the executor sends the PUT twice; the second now carries a
stale version and the server refuses it, so the duplicate never lands and
the row has no landed plants to count. Its receipt reads `HALTED`
(`stale_baseline`) although the first PUT landed; that is an artifact of the
plant, which sends both PUTs inside one write. The probe isn't run by the
adapter on this system: Medplum logs a refused request as an `update`
AuditEvent, which the audit check would count as a second write. Gitea was
not rerun. Its clean writes use no whole-record update or delete route, so
these rules don't change them.

Two changes were needed before the first 22 November run completed, both in
the same pull request as those results:

- Medplum answers an empty search with a Bundle that leaves `entry` out. Since
  #2437, a search answer without its items field is an error rather than an
  empty result, so the first write's marker search failed closed. The pack now
  declares that shape (a searchset Bundle with no next link). Any other
  answer missing its items field is still an error, as is an item with no
  usable id or a reported match the answer doesn't return.
- Medplum's default per-user FHIR quota (50,000 units a minute) was spent by
  audit-log reads within seconds, and a 429 stopped the bench. This bench
  layers [medplum.compose.override.yml](medplum.compose.override.yml) on the
  shared Medplum compose file to raise it; other Medplum benches keep the
  defaults.

Which code produced each file: the Medplum rows were rerun live at commit
`80c3da70` (#2529, enforced preconditions and the closed write-scope check).
Before that they were from commit `6ad9f6fb`. The Gitea rows
are from commit `5680f814`; the later changes touch only the marker search and
the Medplum lab, and Gitea's pack declares no marker search, so its path is
unchanged.

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

- Two systems, one machine (a local Docker host, colima, for the 22
  November run), and synthetic records. Each fault was planted
  50 times, so a detection rate of 50 of 50 bounds the miss rate at about 6%
  (95%) per fault class. The kill criterion is about any miss, not the rate.
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
python benchmark/api_scoped/run.py --system medplum --clean 400 --faults 50 --out benchmark/api_scoped/results/medplum.json
python benchmark/api_scoped/run.py --system gitea --clean 400 --faults 50 --out benchmark/api_scoped/results/gitea.json
python benchmark/api_scoped/lab.py down medplum && python benchmark/api_scoped/lab.py down gitea
```
