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

### The read-back is the last version written

When `_history` is read, the record judged must be the newest version in the
window, matched by `meta.versionId`. It is `needs_review` when the read-back
is an older version, or a version saved after the window closed (by anyone),
or carries no `versionId`; when any version in the window has no
`versionId` or time; or when two versions share the newest time. Without
`_history` there is nothing to bind to, and the other checks decide as
before.

The evidence lists each version in the window as `versions`: its id and its
author, oldest first, and never its body. The author is `meta.author` where
the server returns it. Where it doesn't (Medplum 5.2), and the audit trail
logged exactly one timed create or update per version, the n-th logged write
names the n-th version. Otherwise the author is `null`: unknown, not
guessed.

### When the agent corrects its own record

An agent that reads its create back may find a wrong value (a tool changed
it, say) and fix the record with an update. That is still one record
created once, so it can be `created_verified`, with `corrected: true` and
`writes_to_record` (the create and the updates) in the evidence and the
reason. A person reading the result can see it was corrected.

It counts only when all of these hold; anything else is `needs_review`:

- the audit trail shows exactly one create of the record and then updates,
  no delete, each with a time and each by the agent;
- the record's `_history` is read, and shows as many versions in the window
  as the trail shows writes, so a write the trail didn't log can't be
  hidden among them;
- every one of those versions is the person (by the contract's identity
  rules): a correction may fix a value, never who the record is, since a
  search no audit event names may have found an earlier version. With
  `operations.correction.between: consistent`, every version must instead
  hold only the task's values (see "Operation semantics" below);
- no other actor read the record by id from the create to the window's
  close, and the trail is logging reads by id at all (the verifier's own
  read of the record must show up, as for `found_existing`);
- the contract's `settle_s` is at least 1 s, so the verifier's own reads
  land after the window;
- everything else in the table above: the final record holds every value,
  the agent wrote no other record, and the record is the only one that is
  the person.

What this can't see:

- A read the trail doesn't log by id (a search that returned the record,
  `$everything`, a subscription delivery). The identity rule above limits
  what such a read could have got wrong; a wrong non-identity value (a
  phone number, an allergy's criticality) could have been seen.
- Someone using the agent's own account: their reads and writes are the
  agent's.
- The heartbeat shows the trail logs reads when the verifier reads, not
  that it did throughout the window.

`accept_corrections:
false` in the contract sends every change after the create to review.

### Operation semantics

What one write may also do is contract data, in an `operations:` section.
Every default keeps the rules above, so a contract without the section
judges as before. One principle runs through it: **every version that was
ever live may assert only true facts, and no one but the agent may have
touched it by id.** A search the audit trail doesn't log can have shown any
version, so a version that held a wrong value is never excused, however
briefly it stood.

```yaml
operations:
  correction:
    accept: true            # with accept_corrections; both must be true
    between: either         # or person (the default), or consistent
  cardinality:
    own_transient: true     # default false
  bookkeeping:              # default none
    - resource_type: Practitioner
      paths: [meta.versionId, meta.lastUpdated, "extension[url=urn:app:last-seen]"]
```

The rules belong to the application, not to one task, so they can live in
a versioned spec, `openadapt.app-semantics/v1`, that every contract on the
app pins (example: `benchmark/fhir_medplum/app_semantics/medplum.yaml`):

```yaml
# the contract
semantics: {path: ../app_semantics/medplum.yaml, app: medplum, version: 1,
            sha256: <the spec file's sha256>}

# the spec
schema_version: openadapt.app-semantics/v1
app: medplum
version: 1
resources:                 # resource type -> correction and cardinality
  Patient: {correction: {between: consistent}, cardinality: {own_transient: true}}
bookkeeping: []            # rules as below, for every task on the app
```

A pin that doesn't match the file's `app`, `version` and sha256 (a spec
edited in place), an absolute or missing path, or a contract with both
`semantics` and `operations` doesn't load. The app's bookkeeping rules
apply to every contract on the app except one of the rule's own type. The format
is public; a spec for a customer's system is private, like its other
per-system recipes (`source-policy.yaml`).

- **`correction.between`.** `person` (the default) is the rule above: each
  version in the window is the person. `consistent`: each version holds
  only the task's values, any of them possibly still missing, and nothing
  else: no element the task didn't give, on an open contract too. A
  web form that saves a new record as a create and then updates (Medplum's
  app does: the first version can be `{}`) is one write. A version that held
  another birth date, another patient or a wrong phone number refuses.
  `consistent` is stricter than `person` on values (a corrected phone
  number refuses) and looser on missing ones. `either` accepts a version
  that is the person or holds only the task's values: never stricter than
  `person`, and a record saved in steps still counts as one write. A
  version that names someone else refuses under all three.
- **`cardinality.own_transient`.** A second record of the contract's type
  that the agent created and deleted again in the window (a double submit,
  or a save resubmitted after a lost response, then cleaned up) counts as
  not created, when: no version of any other write in the window refers to
  it (an Encounter or an Observation filed on the duplicate); its every write
  is the agent's, logged and timed, the create first and the delete last; `_history` shows as many versions as the
  trail shows creates and updates; every version holds only the task's
  values; no other actor read it by id; and the contract's audit read has
  `heartbeat` on, so the trail is shown to log reads (the verifier's own read
  of the record that stays). The result says how
  many such records it deleted (`transient_records`).
- **`bookkeeping`.** An update by the agent's actor to *another* record that
  changed only the named elements is the save's bookkeeping, not a write
  (`bookkeeping_writes`). A rule names one resource type, never the
  contract's own (another record of it may be the person), and only these
  paths: `meta.versionId`, `meta.lastUpdated`, and `extension[url=<url>]`
  for one extension by its exact url. Anything else (`meta.security`,
  `meta.tag`, `meta.account`, `meta.profile`, `active`, `link`, a bare
  `extension`) can hide a record, move it, or change a clinical fact, and
  doesn't load. It never applies to the record the task writes, to any
  record that is the person before or after, to a create or a delete, to a
  change the trail didn't log or attribute, or to a change any other actor
  also wrote. `_history` must give the version before the window, and each
  version must differ from the one before only in the named elements. With
  `now_checks: true` (the temporal rule of the whole-database generator's
  bookkeeping answer), every time stamp or date a named extension changes
  to must lie within `now_within_s` (default 60) of the version's own
  server-set `meta.lastUpdated`; a planted or replayed time refuses.
- **Attribution** stays as `self`, `change_feed.audit` and
  `history_author` say: a change no source attributes is the agent's, and
  another actor's change to the created record refuses.
- **Time** stays as `settle_s`, `stable_reads` and the late-write read say:
  the window closes at a server instant, and a write of the agent's that
  lands after it sends the run to review.

The same kinds of incidental change in the whole-database verifier
(`verify.yaml`, [`VERIFY_RUN.md`](VERIFY_RUN.md)): corrections are one
write there by construction (the frame compares the end state with the
baseline); timestamps and version counters are `frame.ignore_fields` and the
profile's column rules; audit rows and per-user rows are the profile's
reviewed bookkeeping tables and `own_rows`, each an exact table, owner and
row relation; attribution is `frame.attribution`; late writes are
`recheck_after_s`. Learned excuses there stay limited to exact table,
column and row relations, and nothing on the written row is excused.

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
| `void` | values that make a record assert nothing (`status="entered-in-error"` on an Immunization). A record holding one is never the person: it is not found, it is not a duplicate, and a created record holding one needs review. It names values to leave out, never values to keep, so a status it misses still matches and stops for review. Each entry is an element path (filters allowed, as in `fields`) and one JSON scalar, compared as JSON (`1` is not `true`). It can't name an identity field. A record that was the person and turns void during the run is not found. A created record the agent corrects must be the person in every version, so a version holding a void value is refused; a status the list doesn't name may still be corrected away | a person, from the resource's FHIR definition |
| `change_feed` | the audit read, `_history`, whether the server returns `meta.author`, sweep types | a person, once per server |
| `accept_corrections` | whether the agent's own updates after its create can count (default true; see above) | a person |
| `operations` | what one write may also do: corrections between `person` and `consistent`, the agent's own transient duplicates, bookkeeping elements of other records (see "Operation semantics"); defaults keep the rules above | a person, from how the app saves |

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
