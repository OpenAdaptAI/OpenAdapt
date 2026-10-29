# Blind measurement: a contract for a write type the generator has never seen

Written and committed before the blind write type was chosen. Nothing here
changes after the first blind demonstration is recorded; anything that has to
change is added below as a dated deviation.

## What is frozen

- **Generator:** `openadapt_flow/generate/ensure_contract.py` and
  `openadapt_flow/generate/fhir_tables.py` at commit `f22e29a3c`. The blind
  run uses that commit's files byte for byte. Any later change to them is a
  deviation and does not count toward this measurement.
- **Verifier:** `openadapt_flow/ensure_record.py` as on `main` at
  `3a3783d03` (#1640). This work does not change it.

## The write type

A separate agent chooses one FHIR resource type from Immunization, Condition,
Observation, MedicationStatement, Procedure and ServiceRequest. It hasn't read
the generator, its tests or this directory. Patient and AllergyIntolerance
are excluded, because the generator's tests use both.

That agent also writes:

- the task, as the parameters an agent would be handed;
- three demonstrations, each a patient seed, the parameters and the resource
  a careful client creates;
- a blind fault suite in #1640's `openadapt.medplum-fault-suite/v1` format.
  It is frozen by sha256 before the generator sees the demonstrations, and
  kept private in openadapt-internal.

## The run

1. On live Medplum 5.2.8 (the #1640 fixture, through the fixture lease):
   record the three demonstrations and capture the teach directory. That is
   the CapabilityStatement, the server's SearchParameter definitions, and
   each demonstration's before and after search. The server facts are
   #1640's probe (`fhir_medplum/readpath/probe.json`), measured once for this
   server image.
2. Run the frozen generator once.
3. **Person time.** A clock starts when the generator's first output is shown
   and stops when the generator admits a contract. Everything a person does
   in between counts: reading refusals, confirming a proposal, editing an
   input or the contract. Each action is logged with its line count and
   minutes. As in `fhir_medplum/readpath/MINUTES.md`, the person is an agent
   and the clock is wall time. **Target: under 2 minutes.**
4. **Refusals.** If the generator refuses, the refusal and the person's fix
   are recorded. A refusal the person can't fix in the inputs is reported as
   is. The person never weakens a check to get past the gate: no
   `compare`, `map` or `allow` the demonstrations don't support, and no
   removed field.
5. **Silent passes.** Run the frozen fault suite against the admitted
   contract with #1640's runner (`readpath/suite.py`'s case executor). A
   silent pass is a case whose right answer is `needs_review`, or a different
   record, that the verifier reports as `created_verified` or
   `found_existing`. Every silent pass is reported, with its class.
   **Target: zero.** False stops (a right answer of `created_verified` or
   `found_existing` reported as `needs_review`) are counted separately.

## Reported

The person minutes, the generator's refusals and confirmations, the
contract's lines with each line's source, silent passes, false stops and
right answers out of the suite size. Per-case values stay private with the
suite.

## Deviations

1. **2026-10-29, before the first blind demonstration was recorded: the
   generator is re-frozen.** An adversarial review (`REVIEW.md`) found four
   ways the generator at `f22e29a3c` could admit a contract that passes a
   wrong record. Each is fixed with a regression test. The generator is now
   frozen at the commit that adds this entry. The blind author's files
   were frozen earlier, and the generator still hasn't seen them. The review
   was given only the code and the existing Patient example, never the
   blind files.
   Disclosure: the generator's author had read the blind author's summary
   (the type is Immunization; its parameter names; its one-sentence
   same-fact rule) before writing these fixes, but not the files. None of
   the fixes is specific to Immunization. The identity proposal rule is
   unchanged except for the floor that finding 3 asked for.
2. **2026-10-29, during the run: where the clock stopped.** The generator
   proposed an identity that left out the date the task's same-fact rule
   needs. The person declined it. The frozen generator takes no other rule,
   so the person edited the contract (2 lines) and the clock stopped at the
   generator's self-check of the edit, not at an admission. The contract was
   not admitted by the generator. `results/PERSON_LOG.md` has the log.
3. **2026-10-29, after the run: type annotations only.** mypy in CI flagged
   the generator. The fix renames two loop variables, narrows one type and
   adds one type ignore. The generator's report on the blind teach
   directory is byte-identical before and after. The measured bytes are
   those at `504eb7bab`.
