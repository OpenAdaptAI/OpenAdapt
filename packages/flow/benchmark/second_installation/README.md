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
| 15 | `PlaywrightBackend.type_text` | A typed ISO date can't fill a native `<input type=date>`: Chromium splits it into locale-ordered segments, `-` doesn't advance, and the year takes six digits | Fixed: an ISO date into a date input is set with `fill` (tested) |
| 16 | `openadapt_flow/risk.py` | Any "Add ..." button reads as a write; Medplum's form has four that only add an empty row in the browser. Each needs an operator classification (`qualify set-risk ... read_only`) before qualification | Open (operator work per app) |
| 17 | Identity and submit binding | **Blocking.** A labeled save button alone at the end of a form gets neither an identity check (its label is excluded as mutable evidence and there is no other row text) nor a submit binding (that applies only to unlabeled controls). Qualification then refuses: "save step has no armed identity check". Medplum's Create is such a button. A fix touches identity, so it needs a design and a safety review | Open; next |
| 18 | `qualify/campaign.py`, `qualify/faults.py` | The web fault driver and its precondition need a CSS selector for the save; the recorder captures one only for a unique `#id`. React apps (Medplum) give role and name only, which the backend's resolver already handles | Open (small fix) |
| 19 | `benchmark/openimis_claims/fixture.py` | Runs `docker compose` against Docker Desktop's socket; on the shared Podman VM it needs `DOCKER_HOST` | Worked around |
| 20 | openIMIS 25.10 images | amd64 only. On the Podman VM without Rosetta (qemu-user), the backend accepts connections and never answers (29 s of CPU in 58 min, before and after a restart) | Environment; the next lease turns Rosetta on |

## Phase A results so far

### Medplum (FHIR): taught, contract written, admission blocked

What worked on the live server (Medplum 5.2.8, synthetic patients):

- **Setup.** The fixture came up from pinned images, about 22 minutes, almost
  all image pulls. It made an admin token, two client applications (record
  check, harness) and a dedicated user through a project invite. The web app
  took the server URL at run time.
- **Teach.** `demo.py` recorded the new-patient form. Three demonstrations
  with different values (two female, one male) each saved exactly one
  Patient. Every field read back right through the Postgres oracle.
- **Compile.** 37 steps, the 10 intended parameters
  (`compiled-program.json`). The compiler dropped two pixel-region checks
  that would have frozen a demonstrated value.
- **Contract.** `verify.yaml` (FHIR `Patient`, matched by email, exact new
  set, identity field `id`) expands to a 10-effect contract with no save
  coverage problems.
- **Phase B inputs.** The three demonstrations carry before and after
  snapshots of the FHIR channel: 0 → 1, 1 → 2, 2 → 3 patients
  (`phase-b-inputs/`).

What the form made the clerk's steps be:

- Gender is a ValueSet autocomplete: type the code, then ArrowDown and Enter.
  Enter alone submits the whole form; an early probe saved three empty
  patients that way.
- Telecom's system is a native select: click it and type the option.

Where admission stopped: `qualify` refused before running a case
(`qualify-refusal.log`):

- save step step_036 has no armed identity check;
- save step step_036 has no structural selector.

Those are bugs 17 and 18. The first is a safety design question, not a
setting: what may stand in for row text when a labeled save is alone on its
row. No soak has run on Medplum yet.

### openIMIS: blocked on the environment

The stack started and its migrations finished, but the backend never answered
under qemu-user emulation (bug 20). Nothing past setup ran. The contract,
hooks, profile and soak adapter are written and unexercised.

### Effort so far

Agent wall-clock minutes from `hours.jsonl`. They include unattended waits
inside setup: about 22 minutes of Medplum image pulls, and most of openIMIS's
setup, which was waiting on the hung backend.

| App | Setup | Teach | Contract | Admission | Total |
| --- | --- | --- | --- | --- | --- |
| Medplum | 28.1 | 12.4 | 1.0 | 3.4 (blocked) | 44.9 |
| openIMIS | 97.6 (blocked) | – | 1.4 | – | 99.0 |

Lines written, the effort measure that doesn't depend on who does the work:

| App | Setup (fixture) | Teach | Contract (verify.yaml, hooks, profile) |
| --- | --- | --- | --- |
| Medplum | 428 | 178 | 299 |
| openIMIS | reused | reused | 319 |

The soak adapters and the claims workload (836 lines) are measurement
apparatus, not installation cost. The product fixes (bugs 7 and 15) are in
`openadapt_flow/`.
