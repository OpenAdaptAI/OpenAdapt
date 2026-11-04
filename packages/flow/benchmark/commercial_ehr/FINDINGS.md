# Qualifying the verifier against a commercial EHR sandbox

Research date: 2026-10-29. Sources are the vendors' own terms pages and the
live sandbox CapabilityStatements, fetched without signing in. Hashes of
what was read are at the end.

## Verdict

| Vendor | Writes in the sandbox (of the four asked) | Change channel | Terms allow publishing and naming | Verdict |
| --- | --- | --- | --- | --- |
| Epic (open.epic / fhir.epic.com sandbox, R4) | Patient create; Observation create and update; AllergyIntolerance create. Appointment is read-only | No AuditEvent search, no `_history`, no `vread`. Provenance and AuditEvent only through `_revInclude` on a search; `_lastUpdated` search on Observation only | Yes, with limits: no benchmarking or confidentiality clause; factual use of the name "Epic" is fair use; no logos and no implied endorsement | **Go** for Phase 2, with the read-path gap below |
| Oracle Health (code console; secure sandbox `fhir-ehr-code.cerner.com`, R4) | All four: Patient create and patch; Observation, AllergyIntolerance create and update; Appointment create and patch. The open sandbox is read-only | No AuditEvent, no `_history`. Provenance read, search and create | **No.** Benchmarking needs Oracle's prior written approval, test results belong to Oracle, and any public announcement about using the APIs is a material breach | **No-go** without Oracle's written approval |
| athenahealth | not assessed | not assessed | not assessed: the developer portal and docs return 403 to automated reads | **Not assessed**; read the terms after signing in, if wanted |

## Phase 2 on Epic: done, on a narrower read path

The founder registered the app on 2026-10-29, and the sandbox accepted its
token at once. What came of it is in [epic/](epic/README.md):

- **Read path, probed** (`epic/probe.json`): search and read work, and the
  server's `Date` header gives the window. There is no AuditEvent endpoint
  (404), no `_history` (404) and no `meta` on records. `_lastUpdated` is
  refused on Patient and silently ignored on Observation: a filter in 2099
  still returned 266 rows, so a sweep there would be wrong, not just
  incomplete. `_revinclude=Provenance:target` returns the latest author
  and time; `_revinclude=AuditEvent:entity` returns nothing. A patient
  search on one value alone matches nothing.
- **What Epic does with a write:** a Patient create needs an SSN. Epic
  answers `201` and **silently drops the phone number** in every format
  tried, while keeping the email. It adds about ten elements of its own
  (a second `official` name, race and ethnicity extensions,
  `meta.security` PATRPT and UNCERTREL). A create of a person who already
  exists, even under a new SSN or at another street, answers `201` with the
  existing record's Location: `201 Created` doesn't mean created. An
  allergy create needs `verificationStatus: unconfirmed`, lands (readable
  by its Location), and **doesn't appear in the patient's allergy search**,
  which still answers "Not on File". An Observation create needs an
  Encounter, which the app has no scope to find.
- **Verifier:** `openadapt_flow.verifier` with the `fhir` record check and
  its existing `smart_backend` auth, contract `epic/register_patient.yaml`.
  The sandbox exposed one verifier bug: the OperationOutcome Epic puts in an
  empty search counted as a record, so a clean create read as a removed
  record. It is fixed in `runtime/effects/fhir.py`, with tests.
- **Fault suites** (`epic/RESULTS.md`): a **blind round** of 13 cases from
  a separate author, frozen by sha256 before its one run, got 12 right with
  no fault passed as verified. The miss was a false refusal of a clean save
  beside a pre-existing look-alike, caused by the contract; the contract was
  fixed to one `exact_new_set` effect, and both suites were rerun fully
  right (not blind). The earlier non-blind rounds got 7 of 9 and 10 of 10.
  The fix to the record check passed an adversarial review
  (`epic/REVIEW.md`).

What a claim from this may say: "a patient registration in Epic's public
FHIR sandbox, read back through FHIR search and read". It can't say that
nothing else changed: the frame is the task's search set, and who wrote the
record isn't checked. `ensure_record` (found or created, with the audit
trail) doesn't run on this sandbox, because its change feed needs AuditEvent
or a working `_lastUpdated` sweep. Allergy writes can't be checked by
search at all; a check would have to read by the Location the create
returns, which trusts the agent's report of the write.

## Epic

### (a) Writes

From `GET https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4/metadata`
(software "Epic", version "August 2026"):

| Resource | Interactions |
| --- | --- |
| Patient | read, search-type, **create**; operations `$match`, `$summary` |
| Observation | read, search-type, **create, update** |
| AllergyIntolerance | read, search-type, **create** (no update) |
| Appointment | read, search-type (no write in R4 or STU3) |
| Provenance | read only |

Other writable types: BodyStructure, Communication, Condition,
DocumentReference, Procedure, QuestionnaireResponse, ServiceRequest (create),
DiagnosticReport, Task (update).

The CapabilityStatement is the server's claim. Each write still needs a probe:
Epic splits APIs by category (for example Observation create for vitals),
and the app must be granted each API on its client ID.

### (b) Change channel

- No `AuditEvent` resource endpoint. Epic's search-parameter documentation
  says `_revInclude=AuditEvent:entity` returns audit events "for a specific
  resource instance" and only "for a subset of FHIR version R4 resources".
  Whether the sandbox populates it is unknown until probed.
- `_revInclude=Provenance:target` is documented for USCDI resources since
  February 2020. Provenance has no search of its own.
- No `_history` and no `vread` on any type (`readHistory: false`).
- `_lastUpdated` is a search parameter on Observation but not on Patient,
  AllergyIntolerance or Appointment, so a sweep can't cover patients.

What this means for `openadapt_flow.ensure_record`: the change feed
(AuditEvent by date, `_history`, the heartbeat) has no counterpart here.
As written, the verifier can't confirm "the agent wrote nothing else", so
every run that needs the feed ends `needs_review`. The record-level checks
still work through search and read: the record exists once, holds every
value, has no extra fields, and no duplicate exists.

Phase 2 confirmed this and checked the narrower path instead (see "Phase 2
on Epic" above): the `fhir` record check of `openadapt_flow.verifier`,
whose frame is the search set. A per-resource `_revinclude` change feed for
`ensure_record` wasn't built: AuditEvent through `_revinclude` came back
empty, and Provenance names only the latest author.

The sandbox is shared with every Epic developer and Epic may wipe it at any
time, so a run must use names and identifiers unique to the run, and a sweep
would see other developers' writes.

### (c) Terms

From the open.epic Terms of Use (fhir.epic.com/Resources/Terms, "Last
updated: October 31, 2025"):

- There is no clause on benchmarking, publishing results, or keeping
  results confidential.
- Names: "If you want to use open.epic or Epic's name or other trademarks,
  you may do so only in accordance with our Trademark Usage Guidelines."
  Those guidelines allow "limited fair use of Epic Marks in referring to our
  company, software, or services without Epic's permission", forbid logos
  without written permission, and forbid uses that could make anyone think
  the product is endorsed, certified, or sponsored by Epic.
- The sandbox "is for testing purposes only" and takes only sample or
  synthetic data.
- The website and Materials "should not be used for performance,
  scalability, or security testing". A small functional suite of our own
  verifier's write checks is none of these, but it must stay small: no
  timing claims about Epic, no load, and no looping over all sandbox records
  (Epic's "Responsible Sandbox Testing" asks for limited windows).
- Epic may collect and report data about connected apps (45 CFR 170.407).
- The open.epic Marketing Guidelines suggest wording such as "[Product] has
  successfully integrated with Epic", ask developers not to speak for Epic,
  and invite drafts to open@epic.com for review (up to two weeks).

So a public line such as "checked against Epic's public FHIR sandbox
(open.epic), through FHIR search and read" is allowed, with no logo and no
"certified" or "validated by Epic". Sending the exact copy to open@epic.com
first is optional but cheap.

### (d) Registration (the founder does this; no agent creates accounts)

Done on 2026-10-29. Two differences from the steps below: Epic checks a
backend app's key against a JWK Set URL, not an uploaded certificate, so
the public key is served as a JWKS from a secret gist; and Epic offers no
Observation Update for vital signs or labs, and no AuditEvent API to backend
apps, so neither was granted.

1. Go to https://fhir.epic.com, choose **Sign Up**, and create an Epic on
   FHIR account with a work email. Confirm the email. Accept the Terms of
   Use when asked.
2. Create a key pair for the app on your Mac (the private key stays outside
   the repository):

   ```bash
   mkdir -p ~/.config/openadapt/epic && openssl req -x509 -newkey rsa:2048 -nodes -keyout ~/.config/openadapt/epic/private.pem -out ~/.config/openadapt/epic/public_cert.pem -days 365 -subj "/CN=openadapt-epic-sandbox"
   ```

3. **Build Apps → Create**. Fill in:
   - Application name: `OpenAdapt verifier sandbox check`.
   - Application audience: **Backend Systems**.
   - Incoming APIs (R4): Patient Read, Search, Create; Observation Read,
     Search, Create, Update (pick the vital-signs or labs variant offered);
     AllergyIntolerance Read, Search, Create; Appointment Read, Search;
     Provenance Read. Add AuditEvent if the list offers it.
   - Default FHIR version: R4.
   - Non-production JWT signing: a JWK Set URL serving the public key
     (Epic requires one for backend apps).
   - Accept the terms checkbox, **Save**, then **Mark Ready for Sandbox**
     if that button is offered.
4. Copy the **Non-Production Client ID** from the app's page.
5. Wait up to an hour: Epic syncs new apps to the sandbox within an hour and
   new APIs within 30 minutes. Calls before that return 403.
6. Add to `/Users/abrichr/oa/src/OpenAdapt/.env`:

   ```
   EPIC_SANDBOX_CLIENT_ID=<Non-Production Client ID>
   EPIC_SANDBOX_PRIVATE_KEY=/Users/abrichr/.config/openadapt/epic/private.pem
   EPIC_SANDBOX_KID=<the kid in the JWKS>
   EPIC_SANDBOX_ALG=RS384
   ```

## Oracle Health

### (a) Writes

From the secure sandbox CapabilityStatement
(`fhir-ehr-code.cerner.com/r4/ec2458f2-1e24-41c8-b71b-0e701af7583d/metadata`):
Patient create and patch; Observation create and update; AllergyIntolerance
create and update; Appointment create and patch; also Condition, Coverage
(with delete), DocumentReference, Encounter, Immunization,
MedicationRequest, Provenance (create) and others. The open sandbox
(`fhir-open.cerner.com`) is read-only.

### (b) Change channel

No AuditEvent, no `_history`. Provenance supports read, search and create,
which is the nearest thing to a change record.

### (c) Terms

"Oracle Terms and Conditions for Oracle Health APIs", effective 3/1/24
(code.cerner.com/terms-of-use redirects to the PDF):

- Section 1(c): you may not "perform or disclose any benchmarking,
  performance or availability testing" of the APIs or products without
  Oracle's prior written approval.
- Section 1(d): "Any test results ... relating to the Oracle Health APIs"
  are Feedback owned by Oracle, and "You shall have no rights to disclose
  such Feedback to any third party."
- Section 8: no use of Oracle Brand Features (trade names included) without
  written permission.
- A later section: a public announcement relating to the use of the APIs, or
  a claim that an app "has been validated by Oracle", without written
  permission is a material breach.

This is the same position as the Citrix trial licence, and stricter: even
running a benchmark needs approval. A write-fault suite of our own verifier
may not count as benchmarking Oracle, but the results couldn't be shown to
anyone, so running it gains nothing for the coverage claim.

### (d) Registration, if Oracle approves

Request written approval first, through Oracle Health's developer contact
(the code console's support link or an Oracle PartnerNetwork contact),
naming the use: a functional test of a third-party verifier's FHIR write
checks in the secure sandbox, with permission to state the result and name
Oracle Health. Only then: sign up at the code console
(code-console.cerner.com), register a **system** (backend) app for the
secure sandbox with the R4 Patient, Observation, AllergyIntolerance,
Appointment and Provenance read and write scopes, and put the client ID and
secret in `.env` as `ORACLE_SANDBOX_CLIENT_ID` and
`ORACLE_SANDBOX_CLIENT_SECRET`.

## athenahealth

Not assessed. `mydata.athenahealth.com` and `docs.athenahealth.com` return
403 to scripted fetches and an empty page in a headless browser, so the
terms couldn't be read without signing in. If the founder wants a third
vendor, they can open the developer-portal terms after signing up and paste
the benchmarking, confidentiality and naming clauses here.

## Evidence

Fetched 2026-10-29, not committed (vendor copyright); the hashes identify
what was read:

| Source | SHA-256 |
| --- | --- |
| Epic R4 CapabilityStatement | `1896b11a32545f5cf568715dadfb2ad5b9fa0ac2b77ec27684637d99d699282a` |
| Epic Terms of Use page (HTML) | `be695a88a84a6fe07299cbd9fecbe1509e3294742b6196dae92514e6133f2b9b` |
| Oracle secure sandbox CapabilityStatement | `ff951024714f2e3247561b969f99668814eb1a8e9b6555e8f5206a5cb560145f` |
| Oracle open sandbox CapabilityStatement | `58c08533a4d0859c15202ff594bb5e031fee4945253bd6b0661ffa13f420e5a8` |
| Oracle Health API terms (PDF, v030124) | `cf9a822bb55311ccb437bc2f0937d281f827b4ff8ebe5f8fb2d4d295e978707f` |

The CapabilityStatements are dated at fetch time; the servers regenerate
them on each request, so the hashes will not reproduce, but the interaction
lists above will until the vendor changes them.
