# Registering a patient in Epic's public FHIR sandbox, checked through FHIR only

An agent registers one new patient in Epic's public sandbox (open.epic,
R4). The verifier (`openadapt_flow.verifier`, `begin_write` and
`verify_write`) reads the sandbox before and after the write with its own
SMART backend token and judges the write against `register_patient.yaml`:
`CONFIRMED`, `REFUTED` or `UNCERTAIN`. It reads Patient searches and
records only. Epic's sandbox has no AuditEvent search and no `_history`, so
the frame is the search set: every patient with the task's family name and
birth date. [FINDINGS.md](../FINDINGS.md) has the terms and the read path.

| File | What it is |
| --- | --- |
| `register_patient.yaml`, `register_patient_phone.yaml` | the contracts, without and with the task's phone number |
| `client.py` | the backend OAuth client (an RS384-signed JWT for a token) |
| `probe.py`, `probe.json` | the live checks of what Epic's read path shows and what it does with a write, and their results on 2026-10-29 |
| `suite.py` | the write-fault suite: ten cases, each a run-unique synthetic patient |
| [RESULTS.md](RESULTS.md), `round1.json`, `round1_followups.json`, `round2.json` | the two rounds, and the reruns between them |
| [MINUTES.md](MINUTES.md) | who wrote what, and how long it took |

## Running it

An Epic on FHIR account and a Backend Systems app with the R4 Patient,
AllergyIntolerance and Observation (vital signs) read and create APIs and
Provenance read. FINDINGS.md has the registration steps. The credentials
live in `.env`, never here:

```bash
cd packages/flow
set -a; source /path/to/.env; set +a
PYTHONPATH=. python -m benchmark.commercial_ehr.epic.probe --out probe.json
PYTHONPATH=. python -m benchmark.commercial_ehr.epic.suite --out results.json
```

The sandbox is shared with every Epic developer, Epic may wipe it at any
time, and the app can't delete. Each run writes about a dozen synthetic
patients with family names unique to the run. The SSNs that Epic's create
requires come from the never-issued 000 area. Don't loop or schedule the
suite: Epic's terms rule out load and performance testing on the sandbox.
