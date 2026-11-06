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

## Checked again later

A write can be right when checked and wrong a minute later: a background
job, a trigger, or the agent's tool writing to another record after it
answered. So each `CONFIRMED` is checked again:

- `fhir`: `recheck_s` after the check (default 300 s), `verify_ensure`
  reads the server again over the window since the first check, with the
  confirmed record as the one already there. It refutes when the record is
  gone, is no longer the person, holds a value other than the one sent (or,
  under a closed contract, a value the agent didn't send), or has a
  duplicate, and when the agent's credential wrote anything since
  that no other write through the shadow explains (a write to the confirmed
  record itself never counts as explained). `--recheck-s 0` turns it off.
- `sql`: when the contract's `frame.database.recheck_after_s` is due,
  `verifier.recheck` reads the whole database again. Later writes through
  the shadow under the same contract are excused as the verifier excuses
  them. A contract without `recheck_after_s` isn't rechecked.

A recheck that doesn't confirm sets the write's final verdict: it moves to
"Landed wrong after the check" (`REFUTED`) or "Couldn't be confirmed"
(`UNCERTAIN`), and both verdicts are shown. The counts are of final
verdicts. A recheck still to run is listed as not yet run; `--until-idle`
waits for the rechecks, while Ctrl-C ends the run without them.

A recheck reads a window, not one record. A change in it by the agent's
credential that no write through the shadow explains refutes every
`CONFIRMED` whose window holds it, not only the write it came from. If the
agent made a later write through the shadow whose record the recheck can't
know (a lost reply, a write under no contract), a refutation that rests on
such a change becomes `UNCERTAIN` instead. A refutation from reading the
record itself (gone, moved, changed) stays `REFUTED`.

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

## Database mode: a client with no HTTP to proxy

A desktop client (a WinForms practice-management app, say) writes to its
database directly, so there is nothing for a proxy to stand in front of. In
database mode (`mode: db`) the shadow has no proxy. It watches the database,
and the agent tells it what it is about to do, before and after the click
that saves:

```
POST http://127.0.0.1:9670/begin  {"action": "create_patient", "values": {"lname": "Quill", ...}}
POST http://127.0.0.1:9670/end    {"action_id": 3, "claimed": "done"}
```

`begin` reads the database before the action (the baseline) and replies
when it has. The reply carries the `action_id` for `end`. `end` has the
action checked as SQL mode checks a write: the action's contract (it must
set `frame.database`) judges what the action should have written and every
other change in the database, and the contract's recheck runs later.
`claimed` is what the agent believes happened. When it isn't `done` and
nothing of the action landed, the verdict is `UNCERTAIN`, not `REFUTED`.

```yaml
schema_version: openadapt.shadow/v1
mode: db
actor: "7"                  # the agent's own user id in the app
audit_sql: "SELECT id FROM audit_log WHERE user_id = %(actor)s"
actions:
  - name: create_patient
    contract: patient.verify.yaml
    params: {lname: lname, fname: fname, birthdate: birthdate}
```

`params` map each contract parameter to a key in `begin`'s `values`, or to
`{literal: value}`.

**Between actions.** The shadow reads the whole database at the start, at
each `end`, after each `begin`'s baseline, and at the stop. When `audit_sql`
is configured, it reads the audit separately before and after each watch
snapshot. A gap compares the audit at the start boundary with the audit after
the ending snapshot, so writes around either separate read aren't silently
treated as clean. The snapshot and audit reads are not one atomic database
transaction. A change in a gap is one no announced action covers:

- `REFUTED` when `audit_sql`, run through a separate read-only connection,
  returns an entry for the agent's own user that wasn't there at the gap's
  start (the agent wrote without announcing it);
- `UNCERTAIN` when the database changed but the audit names no write by the
  agent, when there is no `audit_sql`, or when the audit can't be read;
- nothing, when the database didn't change.

The reads use the contract's own allow-list, ignored columns and app
profile, so the app's own logs don't show as changes. As in the frame, a
read account that can't see the database's triggers (on MySQL it needs the
`TRIGGER` privilege) makes every gap `UNCERTAIN`: a trigger planted between
two reads could pass unseen.

**Overlap.** Actions are bracketed one at a time. An action begun while
another is open or not yet checked makes both `UNCERTAIN`: the database
can't say which change belongs to which. `begin` waits for the action before
it to be checked, for at most `hold_ms` (default 30,000 ms in this mode).

**Guard.** With `--guard`, the shadow is a gate the agent consults:

- `begin` refuses (HTTP 409, `{"refused": why}`) an action it doesn't know,
  one begun while another is open, one whose baseline can't be read, one
  after a change between actions, and any action after a check that isn't
  `CONFIRMED`;
- `end` waits for the check and answers with the verdict and its reasons.

An agent that obeys stops at the first write that isn't right. A recheck
runs later: a write that is `CONFIRMED` and then `REFUTED` on its recheck
halts the guard from the next `begin` after the recheck, not before. The guard
can't stop a client that doesn't ask it: that write shows as a change
between actions.

## Limits

- Database mode sees changes in the database only: a client's write to
  a file, a message queue or another service doesn't show. The whole
  database is read at each `begin`, `end` and gap, so on a large schema
  each read takes longer.
- Only writes that pass through the proxy are seen as the agent's. A write
  the agent makes another way shows only as a change the frame or the change
  feed sees, inside another write's check.
- Checks wait until no write is in flight. Writes of other episodes not yet
  checked are left out of an episode's judgment (each is judged on its own).
  A write the agent makes while an episode is checked makes that episode
  `UNCERTAIN`.
- A change after the recheck's read is outside it.
- A write the agent was asked to make and never made shows nowhere: the
  shadow sees only writes.
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
