# Share a result without sharing the record

```bash
openadapt-flow report-run <run-dir> --receipt share/ --production
```

Writes `share/receipt.png`, `receipt.json`, and `receipt.md` locally and
contacts nothing. Only a `VERIFIED` run may use the success rail, so an
unverified run still emits nothing.

## The receipt is additive, never redacted

The receipt is **generated from a closed allow-list, never redacted from the run
report**. Subtractive redaction of a run report is unwinnable: burned-in pixels,
OCR text captured precisely because it identifies a record, and free-form halt
reasons all leak, and one missed field is a breach. So the receipt declares its
complete field set and refuses any key outside it:

- outcome, profile, and transaction class (closed enums)
- exact authorization / identity / postcondition / effect coverage
- step, heal, and model-call counts
- the zero over-halt counter
- duration
- the resolution-rung histogram
- evidence classes
- substrate
- a validated package version
- the bundle and receipt digests
- explicit provenance
- an hour-truncated timestamp

There is no screenshot, OCR text, typed value, parameter, URL, hostname,
coordinate, workflow name, operator label, or free text.

## Read every byte before you post it

`receipt.json` is every byte that would leave the machine, so you can read it
before you post it. A receipt emitted directly by the bundled tutorial is marked
`synthetic-tutorial` and contains no real data by construction. A separate
`report-run --receipt` invocation refuses to guess provenance: pass
`--production` for a saved run. The `tutorial` command emits its bundled
reference receipt directly; a deserialized report cannot prove that provenance.
Route a production receipt through `sanitize` / `review-sanitized` /
`approve-sanitized` before it crosses a trust boundary. See
[`SANITIZED_ARTIFACTS.md`](SANITIZED_ARTIFACTS.md).
