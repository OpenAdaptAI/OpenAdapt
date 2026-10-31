# Shadow mode

Shadow mode shows a team what their own agent gets wrong on their own
system of record, without changing anything the agent does. It sits beside
the agent, checks each write the agent makes, and writes a report. It never
blocks, edits or resends a write.

```bash
openadapt shadow --config shadow.yaml --out shadow-report
```

The agent then talks to the shadow's address instead of the server's. When
the run ends (Ctrl-C, or `--until-idle`), the report is in
`shadow-report/report.html` and `shadow-report/report.json`.

## How writes are observed

Shadow mode is a pass-through HTTP proxy on the loopback interface. Reads
pass through untouched. For a write (POST, PUT, PATCH or DELETE), the proxy:

1. finds the route the write matches, and the contract for it;
2. reads the system of record before the write (the baseline), for at most
   `hold_ms` (default 2,000 ms);
3. forwards the write and returns the server's reply: the same method,
   target, headers (in order, repeats included) and body. Hop-by-hop headers
   (`Connection`, `Transfer-Encoding` and the like) are the connection's own
   and aren't passed on, so a chunked body goes on with its length instead.

The baseline is the only delay. Each write's added latency is measured and
shown in the report: the hold for the baseline, plus the proxy's own time,
which includes opening a new connection to the server for each request. If
the baseline isn't ready within `hold_ms`, the write goes on anyway and its
verdict is `UNCERTAIN`. If the shadow fails in any way, the write still goes
through and the shadow's error goes in the report. If the server can't be
reached or drops the connection, the agent's connection is closed with no
reply, as it would be without the shadow; the shadow never makes up a status.

Writes that touch the same record (the same contract and identity values,
or a later write to the record an earlier one created) form one episode: a
create, the agent's retry after a lost reply, and its own correction are one
episode. An episode is checked once the agent has made no write for
`quiet_s` (default 5 s), or at the end of the run.

## How each write is checked

Shadow mode has no verifier of its own. It calls the two that exist:

| Mode | Read path | Verifier | Contract |
|---|---|---|---|
| `fhir` | FHIR searches, `_history` and `AuditEvent` | `openadapt_flow.ensure_record` (`verify_ensure`) | `openadapt.ensure-record/v1`, per resource type |
| `sql` | The database, read-only, with the whole-database frame | `openadapt_flow.verifier` (`begin_write`, `verify_write`) | `openadapt.verify/v1` with `frame.database` |

The values the write must hold come from the write itself: each contract
field is read from the request body at the field's path (`fhir`), or from
the body key the route names (`sql`). Shadow mode checks that the system of
record holds what the agent sent, once, and that nothing else the agent's
credential could write moved. It doesn't know what the agent was asked to
do, so it can't tell that the agent sent the wrong value in the first place.

Verdicts use the verifier's vocabulary:

| Verdict | Meaning |
|---|---|
| `CONFIRMED` | The system of record holds the write as sent, once, and nothing else moved. |
| `REFUTED` | It holds something else: a changed value, a second copy, a change to another record, or a value the agent didn't send. |
| `UNCERTAIN` | It couldn't be read, the baseline wasn't ready, the write has no contract, or the agent was still writing while it was checked. |

The verdict fails closed: whatever can't be read is `UNCERTAIN`, never
`CONFIRMED`. A write the server refused, with nothing landed, is
`UNCERTAIN` too: the write isn't there, and nothing landed wrong. Shadow mode
uses no write ledger (`ledger=None`), so it can't hold or refuse any of the
agent's writes. The baselines and receipts go in the output folder.

## Where the contract comes from

A team makes three decisions:

1. **The server.** The upstream URL the agent writes to.
2. **The contracts.** One per kind of write. For FHIR, the reference
   contracts in `benchmark/fhir_medplum/public/contracts/` cover Patient,
   AllergyIntolerance and Appointment, and `openadapt-flow generate
   ensure-contract` writes one for another type from two demonstrations.
   For SQL, a `verify.yaml` with a whole-database frame.
3. **The read credential.** A read-only credential for the verifier,
   separate from the agent's, and the agent's own actor reference.

A write with no matching route is reported as `UNCERTAIN` ("no contract"),
so a missing contract shows in the report rather than hiding a write.

## The report

`report.json` has every write; `report.html` is the same for a person, and
reads in about two minutes:

- the counts: writes seen, `CONFIRMED`, `REFUTED`, `UNCERTAIN`;
- each `REFUTED` write: what the agent sent, what the system of record
  holds now (before and after, with the fields that differ), and the
  verifier's reasons;
- each `UNCERTAIN` write, with what couldn't be read;
- the overhead: added latency per write (median and largest);
- the setup: minutes from start to the first checked write, and the
  decisions it took.

The report holds field names and the values of the writes it describes.
Keep it where the system of record's data may be kept.

## Limits

- Only writes that pass through the proxy are seen as the agent's. A write
  the agent makes another way shows only as a change the frame or the change
  feed sees, inside another write's check.
- Checks wait until no write is in flight. Writes of other episodes not yet
  checked are left out of an episode's judgment (each is judged on its own).
  A write the agent makes while an episode is checked makes that episode
  `UNCERTAIN`.
- A delayed write that lands after the check is outside it. With a
  contract that sets `frame.database.recheck_after_s` (`sql` mode), a
  `CONFIRMED` says it is provisional and when the recheck is due; shadow mode
  doesn't run the recheck itself.
- The server's links (`Location`, `fullUrl`) name the server, not the
  shadow. An agent that follows them goes around the shadow, and those writes
  show only as unexplained changes inside other checks.

## Learning from the run

An app that writes on every request (a user's `last_login`, a token's
`last_seen`) makes the whole-database frame refute every write.
`openadapt-flow learn collect` runs the shadow with a hook that records what
moved, by table and column name. `learn propose` and `learn approve` turn
that into excuses a person approves once. See
[LEARNED_EXCUSES.md](LEARNED_EXCUSES.md).
