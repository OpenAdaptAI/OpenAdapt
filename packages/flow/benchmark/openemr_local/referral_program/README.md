# The certified OpenEMR referral program

The program `openadapt quickstart --app openemr` runs, and the demonstration
it was compiled from. It's the same program behind the site's OpenEMR Seals
(the r3 to r6 runs in
[`benchmark/scoreboard/raw/openemr_referral/certified/2026-10-20/`](../../scoreboard/raw/openemr_referral/certified/2026-10-20/)).
Every patient, address, phone number (555-01xx) and email
(`example.invalid`) is made up.

| File | What it is |
| --- | --- |
| `program/` | The compiled bundle: 29 steps, content digest `10e4594523362a0c79a8a533742a71b5b73178d32378c7bc934b51a2b006bc1c`. It is the October bundle (`ec096afa2b614326b00ec3155f256041c19d86f3998761ce9d772cfd911460b6`, behind the site's Seals) with one change: step_009's date-picker check is compiled against the run's clock (`{Month}-{YYYY}-`) instead of the literal `October-2026-`, which halted every referral from 1 November. Qualified and certified again on 19 November 2026 ([evidence](../../scoreboard/raw/openemr_referral/requalified/2026-11-19/)) |
| `demonstration.jsonl` | The recorded actions it was compiled from: one line per action, with its time, kind, field and typed value |

## How it was made

1. **Recorded** once on the local fixture with the scripted recorder in
   `scripts/openemr_local_demo.py` (`record`), with network capture on. The
   recording's tree hash is `243fb7853b228a2e0fd93c51c1d43e7b84fff567f1aac917bdcd91a54d044894`,
   the bundle's `recording_id`.
2. **Compiled** with `compile_recording` from the same script, with
   `drop_redundant_keys` and `pixel_verify_enabled`. That compile applies the
   shared duplicate rule (`apply_referral_rules`): Create New Patient must
   show OpenEMR's duplicate check with "No matches were found." before the
   program confirms a new chart.
3. **Qualified** (20 October 2026; again on 19 November after the clock
   pass, `python -m openadapt_flow.compiler.clock_pass`) with `openadapt-flow qualify --verify
   benchmark/openemr_local/verify.yaml --certify clinical-write`: 6 of 6
   cases, 3 trials each (representative VERIFIED; ambiguity, wrong identity,
   stale identity, weak effect and missing effect each halted with no write),
   plus 3 of 3 possible-duplicate trials halted before saving. The
   qualification record is inside `program/workflow.json`.

`demonstration.jsonl` is the recording's `events.jsonl` without its page
URLs and titles: those held the recording session's sign-in token. The
screenshots stay out of the repository.

## Using it

`benchmark/openemr_local/quickstart.py` copies `program/` into the
quickstart's output folder and runs it through the certified run path (the
Standard run gate under `clinical-write`, and verify.yaml's REST record
check). Changing any file in `program/` changes its content digest, and
`Workflow.load` refuses a bundle whose files don't match its manifest.
