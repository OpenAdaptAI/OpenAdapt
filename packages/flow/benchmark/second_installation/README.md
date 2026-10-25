# What does a second installation cost?

This directory measures the work to bring OpenAdapt up on a system that isn't
OpenEMR, from nothing to an admitted, running workflow under the production
configuration (Regulated, governed, pinned, durable, pipelined record check,
`problems_only`). There are two systems:

- **openIMIS 25.10** (insurance claims intake; `benchmark/openimis_claims`).
  The record check is SQL on `tblClaim` through a read-only role.
- **Medplum 5.2.8** (a self-hosted FHIR R4 server and web app;
  `benchmark/fhir_medplum`). A Patient is created through Medplum's web form,
  and the record and duplicate checks read the FHIR API.

Phase A is done by hand. Phase B regenerates the parts with
`openadapt-flow generate-parts` once that exists, and compares the two.

## How effort is counted

`hours.py` keeps an append-only ledger (`hours.jsonl`) of timed spans per
app and step:

- `setup`: the test copy;
- `teach`: the demonstration;
- `contract`: verify.yaml, hooks and the signed-in check;
- `admission`;
- `soak`;
- `debugging`;
- `harness`: measurement apparatus a customer doesn't pay for;
- `wait`: unattended runs.

The spans are the wall-clock time of one AI agent doing the work. That is
not an engineer's hours, so beside the minutes the report gives effort
measures that don't depend on who does the work:

- the files and lines written per step;
- each debugging round, with its cause;
- each assumption that broke.

## Assumptions that broke when moving off OpenEMR

Each one is a generality bug. Status: fixed here, or filed.

| # | Where | What assumed OpenEMR | Status |
| --- | --- | --- | --- |
| 1 | `benchmark/lifecycle/run.py` | `--app` accepted only `openemr` and `fake` | Fixed: an app registry |
| 2 | `benchmark/lifecycle/oracle.py` | The 12 New Patient columns, the state map, name and identity keys | Fixed: an app-supplied `OracleSchema` |
| 3 | `benchmark/lifecycle/oracle.py` | Row ids sorted with `int()`; FHIR ids are strings | Fixed |
| 4 | `benchmark/lifecycle/workload.py`, `report.py` | `app="openemr"` written into every item and stop row | Fixed |
| 5 | `benchmark/lifecycle/workload.py` | Every item is a patient referral | Fixed for claims: `workload_claims.py` |
| 6 | `benchmark/lifecycle_proof/run.py` | One verify.yaml, source bundle, user, workflow id, test copy and signed-in check | Fixed: `AppProfile` |
| 7 | `openadapt_flow/runtime/effects/fhir.py` | `extract_path` takes a list's first element only, so it can't tell a Patient's email from its phone in `telecom` | Fixed: `telecom[system=email].value` |
| 8 | `openadapt_flow/qualify/config.py` | `verifier.session` (a hook that makes the client) is allowed only for `kind: rest`; a FHIR or GraphQL channel gets a static token from an environment variable, which can expire in a long run | Open |
| 9 | `openadapt_flow/deployment.py` | FHIR `smart_backend` supports `private_key_jwt` only, not a client secret | Open |
| 10 | `deployment.build_replayer` vs the proof and qualification paths | #1516's record duplicate check is wired only into `openadapt-flow run`; the soak, the proof, resume and qualification use the on-screen check | Reported to the coordinator |
| 11 | `openadapt_flow/runtime/checks/duplicate_record.py` | The REST and SQL duplicate sources and the replayer's duplicate check are OpenEMR patients (`/api/patient`, `patient_data`, `fname`/`lname`/`DOB`); a claim has no duplicate source | Open |
| 12 | `openadapt_flow/policies/` | The only regulated policy is `clinical-write`; a claims system certifies under it | Open |
| 13 | `benchmark/lifecycle_proof/run.py` `LabelAgent` | Repair proposals come from OCR words; openIMIS's Save is an unlabeled icon | Open |
| 14 | Soak injectors | A forced logout ends server sessions; openIMIS uses stateless JWTs, and Medplum's Login resources aren't revocable through its API | Recorded as unsupported per app |

Measured results are added below once each phase has run.
