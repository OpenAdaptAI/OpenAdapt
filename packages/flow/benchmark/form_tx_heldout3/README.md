# Held-out carrier quote portals, set 3

Two synthetic personal-lines quote portals for fictional carriers. They are
served by the Python standard library (`ThreadingHTTPServer`) with plain HTML
and vanilla JavaScript. They form a third held-out benchmark for a
form-filling layer: the layer's authors may look only at each portal's `dev`
case. The harness contract matches `benchmark/form_tx_heldout2`, so a runner
written for that set works here after changing the import.

All names, people, addresses, lenders, boats and policy numbers are invented.

| id | carrier | line | style |
|---|---|---|---|
| `corvane` | Corvane Mutual | homeowners (HO-3) | agent wizard rendered in the browser from a step schema, saved through a JSON API |
| `pellbrook` | Pellbrook Marine | boat and watercraft | consumer conversation, one server-chosen question per card, summary with Change buttons |

## Using a portal

```python
from benchmark.form_tx_heldout3.portals import PORTALS
from benchmark.form_tx_heldout3.scoring import score_case

spec = PORTALS["corvane"]
server, base_url = spec.start()            # host="127.0.0.1", port=0
# POST {base_url}/__oracle/load_case  {"case_id": "<case>"}   (resets first)
# drive the browser from base_url + spec.entry_path; sign in with spec.login
# GET  {base_url}/__oracle/submissions  -> list of stored records
result = score_case("<case>", records)
server.shutdown()
```

`spec.cases` lists the scored cases in the order they must be run.
`spec.dev_case` is the one inspectable case. Intakes are in
`cases/<portal>/<case_id>.intake.json`, and `intake_vocabulary.json` lists
every intake key per portal. Test sign-in credentials are in
`fixtures/portal_logins.json` and in `spec.login`.

Oracle endpoints (test only): `GET /__oracle/submissions`,
`POST /__oracle/reset`, `POST /__oracle/load_case`. Every submission is
stored, including repeats. After a submission, each portal shows the stored
quote and a quote reference.

## Scoring

`scoring.py` documents the normalization and the result fields; it is the
same scorer as the earlier sets. `answer_key.json` holds, per case,
`expected_record` (every non-metadata field the portal stores),
`expected_behavior`, `also_acceptable`, `traps` and `operator_script` (keyed
by visible label and by canonical key, plus `session_login`). The key's
`rule_learning` entry describes the recurring question. `case_defs.py`
documents the behavior labels.

## Reference fillers

`reference_human.py` (careful human, from the answer key) and
`naive_filler.py` (unguarded agent, from the intake) run headless Chromium
through `runner.py`. `record_reference_runs.py` runs both and writes
`REFERENCE_RUNS.json`. `tests/test_form_tx_heldout3_portals_selfcheck.py`
checks the key, the scorer and the reference run. `MANIFEST.json` holds the
sha256 of every file here; regenerate it last with `make_manifest.py`.

Run from `packages/flow`:

```bash
python -m benchmark.form_tx_heldout3.build_cases       # after editing case_defs.py
python -m benchmark.form_tx_heldout3.reference_human --dev
python -m benchmark.form_tx_heldout3.naive_filler --dev
python -m benchmark.form_tx_heldout3.record_reference_runs
python -m benchmark.form_tx_heldout3.make_manifest
```
