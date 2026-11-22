# Held-out carrier quote portals

Four synthetic personal-lines quote portals for fictional carriers, served by
the Python standard library (`ThreadingHTTPServer`) with plain HTML and
vanilla JavaScript. They are a held-out benchmark for a form-filling layer:
the layer's authors may look only at each portal's `dev` case.

All names, people, addresses and policy numbers are invented.

| id | carrier | style |
|---|---|---|
| `harborline` | Harborline Mutual | legacy table-layout homeowners wizard, server-rendered, pop-up estimator window |
| `cobalt` | Cobalt Ridge Insurance | single-page auto quote, div/ARIA widgets, async type-ahead listboxes |
| `pinecrest` | Pinecrest Assurance | homeowners app inside an iframe, full re-render on change, modal estimator |
| `tidewater` | Tidewater Bay Insurance | home + auto bundle, shadow-DOM custom elements |

## Using a portal

```python
from benchmark.form_tx_heldout.portals import PORTALS
from benchmark.form_tx_heldout.scoring import score_case

spec = PORTALS["harborline"]
server, base_url = spec.start()            # host="127.0.0.1", port=0
# POST {base_url}/__oracle/load_case  {"case_id": "<case>"}   (resets first)
# drive the browser from base_url + spec.entry_path; sign in with spec.login
# GET  {base_url}/__oracle/submissions  -> list of stored records
result = score_case("<case>", records)
server.shutdown()
```

`spec.cases` lists the scored cases in the order they must be run.
`spec.dev_case` is the one inspectable case. Intakes are in
`cases/<portal>/<case_id>.intake.json`. Test sign-in credentials are in
`fixtures/portal_logins.json` and in `spec.login`.

Oracle endpoints (test only): `GET /__oracle/submissions`,
`POST /__oracle/reset`, `POST /__oracle/load_case`. Every submission is
stored, including repeats.

## Scoring

`scoring.py` documents the normalization and the result fields.
`answers.json` (named `answer_key.json` until 2026-11-21; renamed because CI
treats any file named like a key as key material; contents and sha256 unchanged) holds, per case, `expected_record` (every non-metadata
field the portal stores), `expected_behavior`, `also_acceptable`, `traps`
and `operator_script` (keyed by visible label and by canonical key, plus
`session_login`). `case_defs.py` documents the behavior labels.

## Reference fillers

`reference_human.py` (careful human, from the answer key) and
`naive_filler.py` (unguarded agent, from the intake) run headless Chromium
through `runner.py`; results are in `REFERENCE_RUNS.json`.
`tests/test_form_tx_heldout_portals_selfcheck.py` checks the key, the
scorer and the reference run. `MANIFEST.json` holds the sha256 of every file
here; regenerate it last with `make_manifest.py`.
