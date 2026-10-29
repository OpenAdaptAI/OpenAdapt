# `ensure_patient_registered` on Medplum, checked through FHIR only

An agent gets a referral and must make sure the person has a Patient record
in Medplum: report the existing one, or create exactly one. The verifier
(`openadapt_flow.ensure_record`, explained in
[ENSURE_RECORD.md](../../../docs/ENSURE_RECORD.md)) decides `found_existing`,
`created_verified` or `needs_review` by reading Medplum the way any FHIR
client can: searches, `AuditEvent` and `_history`. It never reads the
database, and the agent's own report doesn't count.

| File | What it is |
| --- | --- |
| `ensure_patient.yaml` | the contract; each line marked generated or hand-written |
| `generated/` | what `generate-parts` (#1520) wrote from three demonstrations of Medplum's new-patient form, with the demonstrations' values and reads |
| `probe.py`, `probe.json` | the live checks of what Medplum's read path shows, and their results on Medplum 5.2.8 |
| `suite.py` | runs a blind fault suite against live Medplum |
| [RESULTS.md](RESULTS.md) | the blind rounds |
| [MINUTES.md](MINUTES.md) | who wrote what, and how long it took, against OpenEMR |

## Running it

Docker, and a free fixture lease on a shared machine. The fixture pulls
Medplum's pinned images (Apache-2.0; nothing of Medplum is stored here),
binds to loopback, and holds synthetic data only.

```bash
cd packages/flow
python -c "from benchmark.fhir_medplum.fixture import MedplumFixture; MedplumFixture().up()"
python -m benchmark.fhir_medplum.readpath.probe --out probe.json
python -m benchmark.fhir_medplum.readpath.suite --suite SUITE.yaml \
    --sha256 DIGEST --contract benchmark/fhir_medplum/readpath/ensure_patient.yaml \
    --out results.json
```

The suite runner refuses a file whose digest isn't the one given, refuses
to run when the agent's user is an administrator or can write AuditEvents,
and starts each case from a project with no clinical records. The fault
suites themselves are private.
