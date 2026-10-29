# Checking an "ensure the record exists" write through a FHIR read path

`openadapt_flow.ensure_record` judges one agent task, such as
`ensure_patient_registered`: make sure a person has a record, creating one
only if there is none. It reads the server the way any FHIR client can, and
never its database:

- searches, to find every record that is the person, before and after;
- `AuditEvent`, searched by date, for every write in the run window and who
  made it;
- each written resource's `_history`, for its versions in the window and
  their `meta.author`, which the server sets;
- optionally a sweep: a `_lastUpdated` search over named resource types, a
  second source for creates and updates the audit trail didn't log.

The outcome is one of three:

| Outcome | When |
| --- | --- |
| `found_existing` with the id | exactly one record was the person before the run, it still is, and the agent wrote nothing anywhere |
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
| `identity` | which sets of values mean "the same person" | a person |
| `search` | the FHIR search parameter for each identity value | a person (one line each) |
| `self` | the agent's own actor references | setup |
| `change_feed` | the audit read, `_history`, and sweep types | a person, once per server |

## What this read path sees, and what it doesn't

Compared with the whole-database frame on OpenEMR
(`frame.database`, #1582), which compares every row of every table before
and after the run:

| Question | Whole database (OpenEMR, MySQL) | FHIR + AuditEvent + `_history` (Medplum) |
| --- | --- | --- |
| Was the record created once, with the right values? | yes | yes |
| Was a duplicate created, by the agent or someone else? | yes | yes, by search on every identity rule |
| Did the agent write any other resource of any type? | yes, any table | yes, when the server logs the write as an AuditEvent; the sweep adds the types it names |
| Did the agent delete something? | yes | yes from the audit trail; a sweep alone can't see a delete |
| Who made a change? | the app's `log` table, when attributable | `meta.author` (server-set) and the AuditEvent agent, which must agree |
| Bookkeeping the app writes on its own | excused by a reviewed profile (#1604) | not seen at all: FHIR exposes resources, not tables |
| A change to data that FHIR doesn't expose (an app setting, an internal table, a column outside the resource) | yes | **no** |
| A write that bypasses the FHIR layer (a database script, a bulk loader) | yes | **no**, unless it also writes an AuditEvent |
| A server that doesn't keep AuditEvents | n/a | the audit read fails, so every run is `needs_review` (fail closed); a sweep-only feed misses deletes and is stated as such |

So a coverage claim must name its read path: "no other write **through the
FHIR API**, as logged by AuditEvent", not "nothing else changed".

Medplum keeps AuditEvents as resources only when the server sets
`saveAuditEvents` (`MEDPLUM_SAVE_AUDIT_EVENTS=true`); the fixture does.
`meta.author` is evidence only while the agent can't write it: on Medplum a
project administrator can, so the agent's user must not be an administrator.
