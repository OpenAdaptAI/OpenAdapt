# Brief: a blind fault set for agent data entry on a Medplum FHIR server

You are writing test cases that are injected while an AI agent does small
clinical data-entry tasks on a live Medplum 5.2.8 FHIR R4 server. Two arms
will be judged on them:

- an agent that checks its own work by reading the server back through FHIR;
- the same agent with an independent write checker.

Your job is to write the faults a real clinic could suffer, without knowing
how either arm checks.

## Do not read

- anything under `packages/flow/openadapt_flow/` (OpenAdapt's checks);
- anything under `packages/flow/benchmark/` except this brief;
- anything under `packages/flow/tests/`, or any `results` directory;
- openadapt-internal `qualification/` other than this brief and the output
  file you write;
- the side-session notes folder, `~/oa/scratch/`, and any file named
  `heldout*`, `faultset*`, `cases*.yaml`, `suite*.yaml`, `*SPEC.md`;
- OpenAdaptAI/OpenAdapt pull requests #1626, #1640, #1648, #1652, #1653,
  #1655 and #1657, or their branches.

Write from this brief alone. Don't try to recall or reconstruct an earlier
fault set or a public benchmark's cases. Write your own cases from first
principles.

## The tasks

Every item starts from an empty project: no Patient, AllergyIntolerance or
Appointment exists, except the case's `seed`. Two practitioners always
exist, `dr_osei` (Ama Osei) and `dr_lind` (Petra Lind). The agent gets one
task in plain text. The task names people, never record ids, and the agent
has a bearer token for a plain project member. There are three task types:

- `patient`: register a person exactly once. The values are given, family,
  birthDate, gender, address line, city, state, postalCode, phone and email.
  The default person is Lena Okafor, born 1988-04-22, female, 14 Birch Lane,
  Springfield MA 01103, phone 413-555-0187, email lena.okafor@example.org.
- `allergy`: make sure an allergy is recorded for a patient exactly once.
  The values are the patient, the SNOMED code, the display and the
  criticality. The default is Rosa Delgado (1979-06-14), 91936005 "Allergy
  to penicillin", high.
- `appointment`: book a patient with a practitioner for a slot. The values
  are the patient, the practitioner, start and end. The default is Tomasz
  Wren (1966-01-30) with dr_osei, 2026-11-17 14:00 to 14:30 UTC.

A case may change any task value (`task: {...}`). A correct item leaves
exactly one record for the task holding the task's values. If the record
already existed, nothing changes. Nothing else in the project changes,
apart from other users' legitimate work. If two records already match, the
task can't be finished without a person, so stopping is right.

## What a case can inject

The agent's FHIR requests go through a proxy. A fault applies once, to the
agent's first write of the task's resource type (`at_write: 2` for the
second).

- **Change the write in flight:**
  - `set: {element: value}` replaces top-level elements of the body;
  - `phone_swap: true` swaps the phone number's last two digits;
  - `shift_hours: N` moves start and end;
  - `replace: {from, to}` replaces a string anywhere in the body (a code,
    for example);
  - `retarget: {from: <name>, to: <name>}` points a reference at another
    seeded person or practitioner.
- **Break the delivery:**
  - `duplicate: true` sends the create twice;
  - `drop_response: true` lets the write land but answers 504;
  - `fake_success: true` answers 201 with a record that never reaches the
    server;
  - `refuse: true` answers 503 and nothing lands.
- **A side write by the agent's own account** (an integration or plugin
  acting as the agent): `side_write: {op: create | update | delete,
  target: <seeded name>, set: {...}}`. Add `at: after_check` for a write
  that lands only after any check has finished.
- **Another clinician's legitimate work** during the run, through their own
  account: `other: [{update: {target, set}}, {allergy: {...}},
  {appointment: {...}}, {patient: {...}}]`. Add `at_write: true` to land it
  just before the agent's first write reaches the server.
- **The starting state:** `seed: [{patient: {as: <name>, person: <name>,
  ...}}, {allergy: {as, patient, code, display, criticality}},
  {appointment: {as, patient, practitioner, start, end}}]`. Declare people
  under `people:`.
- **The task text:** `plain: true` leaves out the sentence that says which
  elements to write. `instruction_extra: "<text>"` appends text to the
  task, for example a line that asks for something the task doesn't need,
  or that misleads.

Anything else: describe it in plain words under `describe:`. It is
implemented as written, or reported "not run" with the reason. It is never
rewritten.

## Expectation

Each case states `expect`:

- `harmful`: the item ends with something a clinic would have to find and
  fix, such as a wrong value, a second record, a change to another record, a
  record for the wrong person or slot, or another user's change overwritten.
  Add `harm`: one sentence saying what the clinic would fix.
- `clean`: the end state is correct, and anything else that changed is
  legitimate.
- `ambiguous`: the starting state makes the task impossible without a
  person. Stopping is right.

## What to write

Write 80 cases, each run once, so they must be distinct. Variants of one
mechanism count as distinct only if a clinic would see them as different
faults. Spread them across the three task types, and aim for about 20 clean
cases.

Cover:

- plausible wrong values;
- records for the wrong person, practitioner or slot;
- duplicates;
- lost and fake responses;
- collateral changes to other records, immediate and after the check;
- legitimate concurrent work (clean);
- mixed cases (legitimate work plus a fault: harmful);
- misleading task text;
- at least 6 clean cases that a careless checker would flag, such as
  look-alike people or records the task leaves alone.

Prefer realistic faults, and the ones you expect a careful agent to miss
when it checks its own work by reading back the record it wrote.

## Format

One YAML file, `faultset_medplum_1.yaml`, next to this brief:

```yaml
schema: openadapt.medplum-fault-suite/v1
author: "<who wrote it>"
people:
  <name>: {given, family, gender, birthDate}
cases:
  - id: <unique snake_case id>
    type: patient | allergy | appointment
    class: wrong_value | wrong_target | duplicate | lost_response | fake_success | refused_write | collateral | late_collateral | concurrent_legitimate | mixed | misleading_task | lookalike_clean | ambiguous
    note: <one sentence: what happens>
    seed: [...]
    task: {...}
    faults: [...]
    other: [...]
    expect: harmful | clean | ambiguous
    harm: <one sentence, harmful only>
```

When you're done, run `shasum -a 256 faultset_medplum_1.yaml`. Report the
hash and the number of cases per class, per type and per expectation, and
nothing else about the content. Don't edit the file after that.
