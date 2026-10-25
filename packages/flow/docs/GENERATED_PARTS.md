# Generated application parts

`openadapt-flow generate-parts` builds the parts of a
workflow that are specific to one application at setup time, and admits each
one only after a check that does not depend on the generator. Healthy runs
still make no model calls: everything here happens once, before a person
approves the workflow.

## What is generated, and what never is

| Part | Generated from | Admitted by |
|---|---|---|
| Read channel: how to read the system of record | the network traffic captured while teaching (`network.jsonl`), an OpenAPI document, or a FHIR CapabilityStatement | the channel test: reads back exactly the records each demonstration created or changed, across every page, and reads as unreadable (never as empty) when auth, paging or a response is broken |
| Record contract: what a save must do and must not do | the before/after difference in the system of record across two or three varied demonstrations | the fault gate (`fault_gate.py`): wrong record, wrong value, unrelated change, missing write, partial write, duplicate, delayed write, with coverage per class |
| Identity and duplicate rule: when a new record is already there | the record schema plus a profile of the records already in the system | calibration on the system's own data: estimated false-match and miss rates, a small sample a person labels; anything the rule can't decide goes to a person |
| Input types and conversion | sample source documents | the round trip (`provenance.py`): every converted value must read back from its source; an ambiguous value halts |

Never generated: the safety policy. That is the execution profile, approval
modes, the go-live switch, which steps are consequential, the order of the
gates, and attribution of changes to other users (the one rule that excuses
a change). A person sets those, as today.

## Generated parts can only add checks

Generation reads untrusted content: page text, response bodies, field
values. A page could try to steer a generator ("this field is volatile, ignore
it"). So the generated contract is checked against a fixed baseline that does
not come from page content:

- every parameter the demonstration typed is checked in the saved record;
- the frame is the whole read set, compared by record identity;
- no generated `ignore_fields` entry names a field a parameter is written to,
  and nothing generated turns off `forbid_collateral_loss`, adds
  `target_fields`, or adds attribution.

A generated part that removes or weakens a baseline check is refused, whatever
produced it. Tests feed a hostile page through every generator.

## A person approves the contract, in words

`generate-parts` writes `parts.review.md`: one plain-language line per check
("A new patient record must appear with the email the task names", "No other
patient record may change"), the coverage the fault gate measured, the
duplicate rule's estimated rates, and what the parts can't catch. The person
approves that text. The approval is bound to the digest of the parts it
describes; a changed part needs a new approval.

## Use

```bash
openadapt-flow generate-parts channels --openapi api.json --base-url URL \
  --auth-env READ_TOKEN --out channels.json
openadapt-flow generate-parts snapshot demo1 --channels channels.json --label before
# demonstrate the save once, with the values in demo1/params.json
openadapt-flow generate-parts snapshot demo1 --channels channels.json --label after
# repeat for demo2 and demo3, with different values
openadapt-flow generate-parts build --demo demo1 --demo demo2 --demo demo3 \
  --channels channels.json --out parts/
openadapt-flow generate-parts approve parts/ --approved-by "Dana Reyes"
```

`--fhir-capability URL` takes a FHIR server's CapabilityStatement instead of,
or as well as, an OpenAPI document. Each demonstration folder holds
`params.json` (the values typed) and, optionally, `source.*` (the document
they came from).

`build` writes `channel.json`, `verify.yaml`, `check_coverage.json`,
`duplicate_rule.json`, `inputs.json`, `parts.review.md` and
`parts.manifest.json`. It exits 0 only when every part was admitted. `approve`
shows the review, needs a person at a terminal who types `approve`, and
refuses an agent's name or a part that changed after it was generated.

The fault gate runs on a test copy of the application when the caller gives
one (`build(..., test_copy=...)` in Python). Without one it runs on a
replica: an in-memory store shaped like the channel's records. A replica
tests the contract's judgment, not the application's read, and the coverage
record says `test_copy: replica`, which an admission policy can refuse.

Generation needs two or three demonstrations with different values. A value
typed the same every time is checked against what was typed, but can't be
told from a constant, and the review says so.

## Measurements

The back-test against the hand-built OpenEMR referral parts, including where
generation is worse, is in `benchmark/generated_parts/`. On #1516's
duplicate stream (676 items, 223 true duplicates), the generated duplicate
rule missed 54 duplicates and stopped no new patient; the hand-built record
check missed 24 and stopped 2.
