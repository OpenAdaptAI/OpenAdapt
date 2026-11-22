# A real run, handed off and checked by its receiver

`real_handoff.py` connects two parties through a real headless browser run:

- **Party A (sender).** It runs the tutorial program against MockMed in
  headless Chromium (`openadapt_flow.tutorial.run_tutorial`). Flow writes the
  run's `receipt.json` with its run hook (`receipt_after_run`). A then sends
  three claims about the record it entered with `openadapt-flow handoff send`.
  Each claim is bound as the receipt's `payload` subject.
- **Party B (receiver).** B owns MockMed's system of record. It pins A's key
  with `handoff key` and `handoff trust --org "Sender Clinic"`, then runs
  `handoff verify` on each claim. Last, `decide()` reads B's own record
  through MockMed's `/api/db`.

```bash
python real_handoff.py OUT_DIR
```

## Result (2026-11-21, `2026-11-21/evidence.json`)

The run was `VERIFIED`, with 0 model calls, in 26.2 s, and Flow wrote
`receipt.json`.

| Handoff | `verify` | Signer line | Payload bound | B's own record | `decide` |
|---|---|---|---|---|---|
| True claim | intact | Signed by Sender Clinic's key | yes | `recorded_as_intended` | accept |
| Differing claim, one field changed | intact | Signed by Sender Clinic's key | yes | `recorded_differently` | reject |
| Fabricated claim, no such record | intact | Signed by Sender Clinic's key | yes | `not_found` | reject |
| True handoff with other bytes beside it | refused, `payload_mismatch` | | | | |

All three claims carry a valid, bound receipt from a trusted sender. Only B's
read of its own record tells the true claim from the false ones. That read
took 2.3 to 3.7 ms per claim.

**What this is.** One real run on a synthetic app on one computer. Both
parties are local processes with separate seal homes. The claim files hold
MockMed's made-up values. The evidence file lists field names only, never
values.
