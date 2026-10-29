# Checking an "ensure the record exists" write through a FHIR read path

`openadapt_flow.ensure_record` judges one agent task, such as
`ensure_patient_registered`: make sure a person has a record, creating one
only if there is none. It reads the server the way any FHIR client can, and
never its database:

- searches, to find every record that is the person, before and after;
- `AuditEvent`, searched by date, for every write in the run window and who
  made it;
- each written resource's `_history`, for its versions in the window, its
  deletes, and their `meta.author` where the server returns it;
- optionally a sweep: a `_lastUpdated` search over named resource types, a
  second source for creates and updates the audit trail didn't log.

The outcome is one of three:

| Outcome | When |
| --- | --- |
| `found_existing` with the id | exactly one record was the person before the run, it still exists, no other record is now the person, the agent wrote nothing anywhere, and the audit trail logged the verifier's own read of the record |
| `created_verified` with the id | no record was the person; the agent created exactly one; it holds every value; the agent wrote nothing else; nobody else changed it; it is now the only record that is the person |
| `needs_review` with the reasons | anything else, including any read that did not give a complete answer |

The agent's own report is not evidence. A save that landed while the agent
reported a timeout is judged like any other save.

## The contract

`openadapt.ensure-record/v1` (example:
`benchmark/fhir_medplum/readpath/ensure_patient.yaml`):

| Section | What it says | Who writes it |
| --- | --- | --- |
| `fields` | where each task value is stored, and how it compares | generated (`generate-parts`, #1520), then reviewed |
| `identity` | which sets of values mean "the same person" | generated (the calibrated rule), plus a person's rules |
| `search` | the FHIR search parameter for each identity value | a person (one line each) |
| `self` | the agent's own actor references | setup |
| `closed` | the created record holds nothing the task didn't give (a field the task left out must be absent; `meta.security` and `meta.tag` count), except what `allow` names | a person |
| `change_feed` | the audit read, `_history`, whether the server returns `meta.author`, sweep types | a person, once per server |

## What this read path sees, and what it doesn't

Compared with the whole-database frame on OpenEMR
(`frame.database`, #1582), which compares every row of every table before
and after the run:

| Question | Whole database (OpenEMR, MySQL) | FHIR + AuditEvent + `_history` (Medplum) |
| --- | --- | --- |
| Was the record created once, with the right values? | yes | yes |
| Was a duplicate created, by the agent or someone else? | yes | yes, on both the found and the created path. Candidates come from searching each identity rule and each of its values alone; the contract's comparison then decides, over every name and every telecom entry. Names compare exactly after trimming, as the specification says, so "ada quill" isn't "Ada Quill". An email stored in a different case is found only when the server's email search ignores case or another rule finds the record |
| Did the agent write any other resource of any type? | yes, any table | yes, when the server logs the write as an AuditEvent; the sweep adds the types it names. An operation the agent runs (action `E`, such as starting a Bot) counts as its write; an event with no action or an unknown one makes the window unreadable. Writes a Bot makes on its own are logged as the Bot's, so the agent's access policy must not let it run Bots |
| Did the agent delete something? | yes | yes from the audit trail; a sweep alone can't see a delete |
| Who made a change? | the app's `log` table, when attributable | the AuditEvent agent; on Medplum 5.2 nothing else, because `meta.author` is never returned |
| Bookkeeping the app writes on its own | excused by a reviewed profile (#1604) | not seen at all: FHIR exposes resources, not tables |
| A change to data that FHIR doesn't expose (an app setting, an internal table, a column outside the resource) | yes | **no** |
| A write that bypasses the FHIR layer (a database script, a bulk loader, a server admin operation) | yes | **no**, unless it also writes an AuditEvent |
| Writes in a batch, a transaction or a GraphQL mutation | yes | yes: one AuditEvent per resource (probed) |
| A write logged late | n/a | the window closes at a server instant and the feed is read until two reads agree; Medplum logged every write within 0.1 s. Last, the feed is read again with no end: a write of the agent's that landed after the window closed (a save still in flight when its response was lost) sends the run to review. A write that lands after that last read is not seen, so the check runs once the agent has stopped |
| The agent erasing or forging the audit trail | the agent has no database access | yes, **if the agent can't write AuditEvents.** A plain Medplum member can create, change and delete them, unlogged; the deployment must give the agent an access policy that makes AuditEvent read-only (probed: then refused with 403) |
| A server that isn't keeping AuditEvents | n/a | Medplum answers an empty search, not an error, so an empty window proves nothing. A created record must show the agent's own create event. A found record counts only when the verifier's read of it shows up in the trail (`heartbeat`, on by default); otherwise `needs_review`. The heartbeat proves reads are being logged, which is evidence, not proof, that writes are |

So a coverage claim must name its read path: "no other write **through the
FHIR API**, as logged by AuditEvent", not "nothing else changed".

## What a deployment must set up on Medplum

Measured on Medplum 5.2.8 (`benchmark/fhir_medplum/readpath/probe.json`):

- `saveAuditEvents` on (`MEDPLUM_SAVE_AUDIT_EVENTS=true`). Without it there
  are no AuditEvent resources to read.
- The agent's user is a plain member of an ordinary project. Members of
  Medplum's seeded "Super Admin" project can call `/admin/super`
  operations, which change data outside the FHIR API.
- The agent's membership has an access policy that makes AuditEvent
  read-only. Medplum lets any entry that allows a write win, so the policy
  can't say "everything except AuditEvent" with `*`; it names each type the
  server serves (`MedplumFixture.restrict_agent`).
- The verifier's own client reads Patient and AuditEvent, and writes
  nothing.
