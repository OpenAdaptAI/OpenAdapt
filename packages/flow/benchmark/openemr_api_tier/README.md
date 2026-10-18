# API steps on the local OpenEMR referral

Evidence for [API steps from a recording's network traffic](../../docs/API_TIER.md),
on a local OpenEMR 8.0.0.3 test copy with synthetic patients only.

| Folder or file | What it is |
| --- | --- |
| `feature_e2e.py` | The shipped feature end to end: record the referral with `NetworkCapture`, compile, propose and admit the API step, then replay the same bundle with API steps off (`browser`) and on (`api`) |
| `scoreboard_arm.py` | The same runs as scoreboard rows: arm `program` (API steps off) and arm `program_api` (on), task `openemr_referral`, under one run id |
| `spike/` | The 18 October spike that came first: a separate prototype with its own sign-in, its 20 + 20 runs, and the probes of what the server does and does not enforce |
| `spike_fixture.py`, `common.py` | The private OpenEMR fixture (ports 9811/9810) and the eight referrals, shared by both |

All patients, addresses, phone numbers (555-01xx) and email addresses
(`example.invalid`) are made up.

## Results

The feature's numbers are scoreboard rows (`benchmark/scoreboard`, task
`openemr_referral`, arms `program` and `program_api`), run under the
scoreboard's protocol and load gate. The spike's numbers in `spike/` used a
standalone sign-in, not the replaying browser's session, and are not the
feature's.

What the feature run established so far, on the shipped code: the
demonstration recorded with capture (28 requests), and the compiler proposed
the API step for the save. It explained all 105 fields of the request, carried
the duplicate check as a pre-check, and copied 11 record-check effects. In a
first smoke run on an earlier commit, the API step saved two referrals 12 of 12
through the live browser session, and the same bundle with API steps off ran
on the GUI. Those two runs are a smoke test, not a measurement.

## How the runs are made

- **Recording**: a scripted person registers referral fax A (Elena Marsh) in
  a new browser session with `NetworkCapture` attached before sign-in, so the
  New Patient form page is in the capture. The harness checks that the
  demonstration itself saved the referral 12 of 12 before it compiles. Under
  heavy machine load, one demonstration typed "Ms." into the first-name
  field; the compiler then refused to propose an API step because the save
  sent a value it couldn't explain, and the recording was discarded.
- **Compile**: `openadapt-flow`'s compiler through the OpenEMR harness, which
  adds the record check (11 effects) and the referral's rule that the
  duplicate check must report "No matches were found." Because the rule is
  added after compile, the harness proposes the API step again
  (`propose_api_step(replace=True)`). The harness admits it under the name
  `benchmark-fixture`. In use, a person admits it with `openadapt-flow api
  admit`.
- **Runs**: the same bundle, replayed by the same engine with
  `Replayer(api_steps=False)` (`browser`) and with API steps on (`api`). Five
  blocks of four referrals per arm; each block resets the database and seeds
  the existing chart Rosa Delgado 1958-03-27. Arms are interleaved and
  alternate order by block. r6 (Rosa Delgado, a different birth date) is the
  possible duplicate; the correct result is no new chart.
- **Clock**: a new browser context, then sign-in and opening the form
  (`setup_s`), the engine (`engine_s`), and the record check (`readback_s`:
  a 1.0 s settle plus SQL). The record check is the test setup's own SQL
  read-back, the same rule as the October timing study: one new chart
  matching all 12 fields, or no new chart for r6. The engine also confirms
  each save through its own REST verifier.
- **Load gate**: each run waits until the shared machine's 1-minute load is
  under 12 (`gate_waited_s`); `gate_gave_up` marks a run that started after
  waiting the maximum.

```bash
export PYTHONPATH=packages/flow DOCKER_HOST=<podman socket>
python -m benchmark.openemr_api_tier.spike_fixture create   # once
python -m benchmark.openemr_api_tier.feature_e2e record --out <rec>
python -m benchmark.openemr_api_tier.feature_e2e compile --recording <rec> --out <bundle>
python -m benchmark.openemr_api_tier.scoreboard_arm --bundle <bundle> --run-id api-tier-<date> --work <dir>
```

The recording and bundle are not committed. The recording holds the
synthetic patient's values and the capture. The OpenEMR compose file is
fetched at run time by `../openemr_local/upstream.py`.
