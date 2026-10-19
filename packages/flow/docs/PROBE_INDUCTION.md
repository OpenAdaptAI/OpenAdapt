# Settle a disagreement between recordings on a test copy

`induce` learns one program from several recordings of a task. When one
recording has a consequential step that another lacks, the recordings alone
can't say whether the step was noise or a branch the task needs. Plain
`induce` refuses and names the recording that would settle it, for example
"Record the path where Sign chart is absent."

Recording more clean, agreeing demonstrations is slow. `induce --probe-on`
lets a test copy of the app answer the question instead. It's opt-in, and it
runs only on a target the deployment file declares a test copy.

Status: in review. Tested in CI on MockMed, OpenAdapt's synthetic test app,
with its transactional server as the system of record. It has not run
against a third-party application yet.

## How it works

For each refused point, probing tries two readings:

| Reading | What every recording replays |
|---|---|
| `incidental` | The recording without the step. |
| `conditional` | The recording with the step, set to run only when its target (or its dialog) is on screen and to skip otherwise. This is the guard plain `induce` would put on the step. |

Each reading replays in every recording's context: that recording's own
steps, values and subject, with only the point in question changed. Before
each run the test copy is reset to its baseline, and each run starts a new
session.

Each run is scored by reading the system of record back, with the same judge
`serve-reward` uses (`openadapt_flow.reward.worker.judge_episode`) and the
oracle the reward contract names. A run counts only when the judge says
`verified` for that recording's subject: every required effect is present
and no forbidden effect is.

A point is certified only when exactly one reading produced the recorded
effect in every context and the other reading conclusively failed in at least
one. In every other case the point stays refused, with its record-next item,
exactly as without probing:

- Both readings produced the effect. The probe can't tell them apart.
- Neither did.
- A read-back couldn't decide (store unreachable, effect uncertain, replay
  crashed). Such a run is logged as inconclusive and never counts as a miss.

Probing covers the points plain `induce` reports as `unconfirmed_optional`
and `unconfirmed_branch`. Other refusals (contradicting paths, step order,
loops with a consequential body) are logged as not probed and stay refused.

## Examples

These are the three MockMed cases in `tests/test_probe_induction.py`.

- **Incidental click.** Recording A clicked Sign chart on a chart that was
  already signed, so the button did nothing. Recording B didn't click it. On
  the test copy, the `conditional` reading signs B's chart, which writes a
  signature record the reward contract forbids. Only `incidental` holds, so
  the program drops the step.
- **Real branch.** Recording A's patient opened behind an allergy alert, and
  the person pressed Accept. Recording B's patient had no alert. The
  `incidental` reading leaves A's Save button behind the alert, and the run
  halts before saving. Only `conditional` holds, so the program gets a branch
  guarded on the alert.
- **Can't decide.** Recording A clicked Print summary. Printing writes
  nothing to the record, so both readings produce the recorded effect. The
  point stays refused.

## Declare the test copy

Probing reads one deployment file. It refuses, before it resets or replays
anything, unless that file has a `test_copy` section like this one:

```yaml
name: mockmed-test-copy
backend:
  url: http://127.0.0.1:8765/
test_copy:
  declared: true
  reset_url: http://127.0.0.1:8765/api/reset   # or reset_command: [argv...]
  reward_contract: contracts/chart-note          # serve-reward bundle
```

| Field | Meaning |
|---|---|
| `declared` | The operator's statement that every system the file names is a test copy. Must be `true`. |
| `reset_url` | Probing sends an HTTP POST here before every run to restore the baseline. |
| `reset_command` | Or a command, as an argv list run without a shell. |
| `reward_contract` | A reward contract bundle in the `serve-reward` format. Its required and forbidden effects define the recorded effect; its oracle recipe is the read-back. A relative path resolves against the deployment file. |

Probing also refuses when:

- there's no reset or no reward contract,
- `runtime.profile` is `standard` or `regulated` (production profiles), or
- the backend attaches to an already-open browser
  (`browser_cdp_endpoint`, `browser_page_url`), since each run needs a fresh
  session.

The reward contract's identity keys (for example `patient_id`) must be in
each recording's `params`, so each run is judged against that recording's own
subject. A recording without them isn't probed.

## Run it

```bash
openadapt-flow induce rec-a rec-b --out program \
  --probe-on test-copy.yaml --report induce-report.json
```

The output lists every probe run and one verdict per point:

```text
probes on test copy 'mockmed-test-copy': 4 run(s)
  [probe]     opt_2 incidental in rec-a: verified (completed) -- every required effect is present exactly as declared
  [probe]     opt_2 incidental in rec-b: verified (completed) -- every required effect is present exactly as declared
  [probe]     opt_2 conditional in rec-a: verified (completed) -- every required effect is present exactly as declared
  [probe]     opt_2 conditional in rec-b: wrong_effect (completed) -- a forbidden effect is present in the system of record
  [verdict]   opt_2: certified incidental [incidental=passed, conditional=failed] -- only incidental produced the recorded effect in all 2 recording contexts
Certification: CERTIFIED
```

Once a reading conclusively fails in one context, probing doesn't run it in
the remaining ones.

`--report` writes the same result as JSON, including every run's reading,
context, runtime signal, judge outcome, reason and model-call count.
`--probe-work-dir` keeps the staged bundles and run directories (screenshots
and replay reports); by default they go to a temporary directory that is
removed afterwards.

Exit codes: 0 when the program is certified and written, 2 when a point stays
refused, 3 when the target is refused.

## What probing will not do

- **Probe production.** The `test_copy` declaration is the only thing that
  opens the gate, and a production profile closes it again.
- **Let a model decide.** A `Proposer` stays advisory: its proposals are
  recorded as untrusted, and probing never reads them. The test
  `test_proposer_stays_advisory` gives a proposer that argues for the wrong
  reading; the read-back decides.
- **Call a model by default.** Probe replays run with model grounding and
  egress off. Each run's model-call count is in the report.
- **Hide a run.** Every run is in the report, including inconclusive ones and
  runs that didn't change the outcome.

## Limits

- One point at a time. While a point is probed, every other point keeps what
  each recording did.
- Only two readings per point. A step that should run under a condition other
  than its own target being on screen (say, a field value) needs a recording,
  as before.
- The check is only as good as the reward contract. A contract with no
  forbidden effects can't tell a harmless extra write from a harmful one.
- The CI tests drive the real replayer against a scripted MockMed screen. The
  browser path (`DeploymentProbeTarget` for `web` backends) isn't covered
  end to end yet.

## Code

- `openadapt_flow/compiler/probing.py`: `probe_and_induce`,
  `probe_target_refusals`, `DeploymentProbeTarget`
- `openadapt_flow/compiler/induction.py`: `optional_points`,
  `optional_guard`, `ProbeRun`, `ProbeVerdict`
- `openadapt_flow/deployment.py`: `ProbeCopySection` (`test_copy`)
- `tests/test_probe_induction.py`
