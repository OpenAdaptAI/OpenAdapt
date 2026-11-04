# Person log: B's three contracts for run 3

Generated on 2026-10-30, under the lease, after the blind fault set was
frozen (openadapt-internal `qualification/incremental-value-3/`, commit
14a055c, sha256 `124a53d7…`). The person hadn't read the fault set. The
generator is the one on `main` (`openadapt_flow/generate/ensure_contract.py`
at `e5a412d35`, #1653). The demonstrations are `teach_tasks/*.yaml`, written
before the fault set existed and recorded on live Medplum 5.2.8 with
`contract_autogen/blind.py teach`.

The clock is wall time in UTC. The person is an agent that knows the
contract format and had seen the public benchmark's hand-written
Appointment contract, so it knew the `^=` filter existed. A person new to
the format would take longer.

| Time | Action | Lines | Seconds |
|---|---|---:|---:|
| 02:12:43 | The generator's first output, for all three types. **Patient: admitted** (the reviewed template; 3 demonstrations and 10 perturbations each). **Allergy: refused** (`code.coding.display` holds values no task parameter explains) and identity to confirm. **Appointment: refused** (the patient and the practitioner are in a `participant` list whose elements can't be told apart), and no identity | 3 read | 0 |
| 02:12:43–02:13:11 | Read the reports. Decided that the display is a task value (the task names it), so it belongs in the demonstrations' parameters | 0 | 28 |
| 02:13:11 | Added `display` to the three allergy demonstrations' parameters (an input edit) and ran the generator again. No refusal; identity to confirm: `[clinical_status, code, criticality, patient, verification_status]` | 3 edited | 1 |
| 02:13:12–02:13:27 | Declined both identities and wrote the person's lines. **Allergy:** the same allergy is the same patient and substance, and criticality and statuses are values (`identity [patient, code]`, `search` for both); `void: verificationStatus = entered-in-error` (FHIR R4). **Appointment:** `patient` and `practitioner` paths filtered by reference type (`participant[actor.reference^=Patient/]`); `compare: instant` on start and end; `identity [patient, start]` with `search patient`; `allow participant.status = accepted`; `void: status = entered-in-error, cancelled` (FHIR R4: neither is a booking) | 13 written | 15 |
| 02:13:27–02:14:02 | Ran the generator's self-check on the edited contracts: allergy passes (3 demonstrations, 7 perturbations each). For the appointment, the self-check's perturber can't follow the `^=` filter, so the person changed each reference by hand. A changed patient or practitioner reads as wrong, swapped participants read as right, and a second patient participant is caught by the closed record | 0 | 35 |
| | **Total** | **3 inputs edited, 13 contract lines written** | **79** |

**The generator admitted one of the three contracts.** For the other two,
its gate held. It wouldn't admit an identity it had no reviewed template
for, and it refused a list it couldn't tell apart. A person wrote what it
couldn't.

`costs_3.json` charges B's setup as 79 s (1.32 minutes) of contract time,
plus 35 minutes of read-path qualification in the primary figure.
