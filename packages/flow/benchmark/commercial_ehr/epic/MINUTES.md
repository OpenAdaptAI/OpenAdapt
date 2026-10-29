# Who wrote what, and how long it took: Epic against Medplum

Agent wall-clock on 2026-10-29, from command timestamps in the session
(12:41 to 13:05 EDT), rounded. The person's part was the registration
(FINDINGS.md, section (d)), which took them a few minutes and isn't counted
below.

## The contract

`register_patient.yaml` has 46 lines that aren't comments, all written by
hand. The #1520 generator (`generate-parts`) wasn't used: it learns field
paths from demonstrations of an app's form, and Epic's sandbox has no form
to demonstrate, only the FHIR API. The field paths are the US Core Patient
paths, the same as in Medplum's generated contract, so writing them took
minutes.

| Part | What it took | Minutes |
| --- | --- | --- |
| Verifier block: base URL, SMART backend (client ID, kid, RS384 key from `.env`), resource type | the product's `smart_backend` config, unchanged | 2 |
| Search: family name and birth date (Epic refuses a search on one value) | one probe, two lines | 1 |
| Field paths and effects: name, birth date, sex, street, city, state, postal code, email | copied from the US Core paths | 3 |
| Phone variant | one line each in a second file | 1 |
| **Contract total** | | **about 7** |

## The read path

| Finding | What it took | Minutes |
| --- | --- | --- |
| No AuditEvent, no `_history`, no `meta`; `_lastUpdated` refused on Patient and ignored on Observation | read-only probe, 14 calls | 3 |
| A create needs an SSN; Epic drops the phone and adds about ten elements of its own | write probe | 4 |
| A create of a person who exists answers 201 with the old Location | write probe | 2 |
| An allergy create lands but doesn't show in the patient's allergy search | write probe | 4 |
| The empty-search OperationOutcome counted as a record | a product fix (one branch) and two tests | 6 |
| **Read-path qualification total** | | **about 19** |

The suite and the two rounds took about 10 more minutes, including the
case rewrites in RESULTS.md.

## Against Medplum

| | Medplum (FHIR + AuditEvent + `_history`) | Epic sandbox (FHIR search and read) |
| --- | --- | --- |
| Contract lines written by hand | 16 (14 generated) | 46 (no generator: no form to demonstrate) |
| Agent minutes on contract and read path | about 43 | about 26 |
| What the frame covers | every write the audit trail logs, any resource type | the search set only: patients with the task's family name and birth date |
| Who wrote a change | the AuditEvent agent | not checked; Provenance names an author only for the latest change |
| Fault rounds | 3 blind rounds by separate authors | 2 rounds by the contract's author (not blind) |

Epic took fewer minutes because there was less to qualify, not because it
is better: the read path that exists is narrower. Any claim from this run
names that path: "the patient record, read back through Epic's FHIR search
and read".
