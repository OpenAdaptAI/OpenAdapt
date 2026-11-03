# Person log: blind run 1 (Immunization)

The clock is wall time (UTC, 2026-10-29), as `BLIND.md` step 3 states. The
person is an agent: the generator's author, who knows the contract format.
A person new to the format would take longer to find the edit.

| Time | Action | Lines | Seconds |
|---|---|---:|---:|
| 20:38:52 | The generator's first output: no refusals, one proposal to confirm, `identity: Immunization has no reviewed template; proposed ['patient', 'vaccine_code']` | 1 read | 0 |
| 20:39:10 | Read the report and the proposed contract | 50 read | 18 |
| 20:39:10–20:40:03 | Compared the proposal with the task's same-fact rule ("same patient with the same CVX vaccine code given on the same date"). The proposal leaves out the date, so a 2024 flu dose would read as a 2025 one. Declined it. Checked how the generator takes a different rule: it has none, only confirm or decline. | 0 | 53 |
| 20:40:03–20:40:10 | Edited the contract: `occurrence_date` added to the identity rule, and `search: occurrence_date: date`. Ran the frozen generator's self-check on the edit: 3 demonstrations read back right and closed; 5 field perturbations and one extra element caught in each. | 2 edited | 7 |
| | **Total** | **2 lines edited** | **78** |

**The generator did not admit a contract.** Its gate held: it wouldn't admit
the wrong identity without a person, and the person declined it. The
contract the suite ran is the generator's output plus a 2-line edit by the
person, which BLIND.md step 3 allows ("editing an input or the contract").
The clock stops at the self-check of the edited contract instead of at an
admission. That is a deviation from step 3's stopping point, recorded here.

What the contract can't say: the same-fact rule also counts only
*completed* records ("an entered-in-error record asserts nothing"). An
identity rule in `openadapt.ensure-record/v1` names task fields only, so a
status filter can't be written into it. That isn't changed here: this work
doesn't touch the verifier.
