# Protocol: generated verifiers vs hand-written verifiers under planted faults

Written and committed before any trial below was run. Changes after the first
measured run go in a dated "Deviations" section at the end, never in place.

## Question

Can the existing generation paths build a record verifier (a `verify.yaml`
effect contract) from what one correct run observed, with no person writing
any of it? And does that verifier accept and reject the same runs as the
hand-written `verify.yaml` when the app plants a bad write?

## Apps

Both are OpenAdapt's own synthetic fixtures. All data is fake and nothing
leaves localhost.

- **MockMed** (`openadapt_flow.mockmed.fault_server`): chart an encounter.
  Hand-written verifier: `environments/openenv/mockmed/verify.yaml`.
- **MockLoan** (`openadapt_flow.mockloan.fault_server`): authorize a loan
  disbursement. Hand-written verifier:
  `environments/openenv/mockloan/verify.yaml`. **Held out:** no engine or
  generator code changes for this benchmark at all, and the harness has no
  MockLoan-specific code; MockLoan appears only in the app spec data
  (`apps.py`).

## The correct run that generation sees (the demonstration)

One run per app, fault mode `ok`, on a freshly reset store with no seeded
rows. A headless Chromium drives the app's own task solution (the selector
steps from `environments/openenv/make_mock_tasks.py`). The harness writes the
recording the way the recorder lays it out: `events.jsonl` (one event per
step, the Save/Authorize click labelled with its button text), `meta.json`
(the run's parameter values) and `network.jsonl`
(`openadapt_flow.network_capture.NetworkCapture` on the browser context, the
code behind `record --capture-network`).

## Arms

Every generated file is used after mechanically deleting its
`needs_operator_confirmation: true` lines. That measures what an operator who
accepts the proposal without reading it would get. The generator's own label
(working or draft) is recorded beside it.

Zero-config (nobody writes any verifier text):

| Arm | Inputs |
|---|---|
| `Z-net` | `scaffold-verifier` on the recording of the app's own traffic |
| `Z-net-read` | same, but the demonstration also loads the store's record list (`GET /api/db`) in the page before and after the save. Assisted demonstration, no verifier text |
| `Z-db` | `scaffold-verifier --db <the app's SQLite file>` (only an app whose store is a file) |
| `Z-db-gate` | `Z-db` plus `--fault-gate` |

Declared (one yes/no statement by a person, no verifier text):

| Arm | Inputs |
|---|---|
| `D-net-read-complete` | `Z-net-read` plus `--complete-read` |

Read path given (a person writes only the `verifier:` read path, copied from
the hand-written file; the effects are generated). Not zero-config; measured
to show how far one read path gets:

| Arm | Inputs |
|---|---|
| `P-record-verify` | `record --verify` (`openadapt_flow.record_sor.RecordingObservation`) around the demonstration: `verify.proposed.yaml` |
| `P-scaffold-verifier` | `scaffold-verifier --verifier <read path>` on the recording, with `sor_snapshots.json` and the Save event's `sor_before`/`sor_after` as `record --verify` writes them |

An arm whose generator refuses, or writes only a TODO draft that
`verify-run` cannot parse, has **no runnable verifier**. That is reported as
a gap with the generator's message; it gets no accept/reject counts.

## Trials

Each trial: reset the store (optionally seeding a concurrent actor's row),
read the baseline through every verifier under test, drive the app's own task
solution in headless Chromium with `?fault=<mode>`, then judge with
`openadapt_flow.verify_run.verify_run` (through
`openadapt_flow.rl_env.reward.EpisodeJudge`, the RL environment's judge).
A verifier **accepts** a run when the verdict is `CONFIRMED`. `REFUTED` and
`INDETERMINATE` are rejects (fail closed); `INDETERMINATE` is counted
separately as well.

Planted faults are the fault servers' own modes:

- correct runs: `ok` (with and without a seeded concurrent row on another
  record), `idempotent` (sent twice with a key, kept once), `timeout` (lands,
  then the client times out);
- wrong writes: `partial`, `optimistic`, `session`, `duplicate`, `double`,
  `stale` (target record seeded, so the seeded row is lost), plus
  `no_persist` and `wrong_record` (MockMed only) and `collateral` (MockLoan
  only).

Each fault runs on 3 cases with parameter values that differ from the
demonstration's (other patients or loans, other types or products, other
notes or memos). `ok` runs on 6 cases. Every case is listed in `apps.py`.

## Ground truth

Independent of every verifier: the harness reads the fault server's own store
(`db.snapshot()`) before and after the trial. A run is correct when exactly
one record was added, it holds the trial's parameter values (on the
disbursements surface, for MockLoan), and every record that was there before
is still there unchanged. The harness also checks this against the fault
mode's design label (`FAULTS` in `make_mock_tasks.py`) and reports any trial
where the two disagree.

- **False accept:** ground truth says wrong, the verifier accepts.
- **False reject:** ground truth says correct, the verifier rejects.

## Kill thresholds (per app, per arm)

A generated verifier is **not ready** on an app if any of these holds:

1. the arm produced no runnable verifier;
2. its false accepts exceed the hand-written verifier's false accepts on the
   same trials;
3. its false rejects exceed twice the hand-written verifier's. When the
   hand-written verifier has 0 false rejects, any generated false reject
   fails this test.

Zero-config verification is **ready on these fixtures** only if at least one
zero-config (`Z-*`) arm passes all three on both apps, including the held-out
MockLoan. A `D-*` or `P-*` arm passing does not count toward that verdict; it
is reported as how much human input closes the gap.

## Paths named in the brief and not measured as arms

- Chrome extension network-save capture (#2014, `extension_network.py`)
  records writes only, with no read responses. Whether `scaffold-verifier`
  reads `extension_evidence.jsonl` at all is checked and reported.
- `benchmark/contract_autogen` generates FHIR `ensure` contracts for Medplum,
  not a `verify.yaml` for these apps. Reported, not run.
- The compiler's effect mining from `sor_before`/`sor_after` produces bundle
  postconditions, not a `verify.yaml`. Its scaffold-verifier counterpart (the
  TODO `effect_contract.yaml` draft) is exercised by `Z-net` when no read is
  found.

## Outputs

`results.json` (every trial, every verdict, the generator outputs with the
local port and file paths replaced by placeholders), `RESULTS.md`, and the
generated verifiers under `generated/`. Re-run with:

```bash
cd packages/flow
uv run --frozen --extra dev --extra browser python -m benchmark.zero_config_verify.run
```

## Deviations

### 2026-11-09, before any trial (found by the generation-only smoke run)

The committed hand-written files (`environments/openenv/{mockmed,mockloan}/verify.yaml`)
read `/api/db` without declaring the read complete. Since #2041 a REST read
whose completeness is not established is unreachable, so on this commit both
files return `INDETERMINATE` for a correct save (checked by hand on one MockMed
`ok` save: `INDETERMINATE`, outcome `reconciliation_required`; with
`records_complete: true` added: `CONFIRMED`). A baseline that rejects every
run makes the false-reject threshold meaningless, so there are two hand-written
baselines:

- `hand-written` (as committed): reported, not used for the thresholds;
- `hand-written+complete`: the same file with `records_complete: true` added
  under `verifier:`, the one line #2041 made necessary. **The kill thresholds
  compare against this one.**

The `P-*` arms take their read path from `hand-written+complete` for the
same reason. No other change.

### 2026-11-09, before the second run (generator fixes)

The first run found three generator gaps (RESULTS.md, "Why each zero-config
arm failed"). The engine change `feat/zero-config-read-discovery` closes them
in app-generic code: a generated REST read carries `records_complete: true`
when it is declared or one unpaged response was seen whole; a step is a write
when the capture shows a mutating request to the app's origin or its store
snapshots differ; and `scaffold-verifier --probe-reads` probes the GET reads a
captured write implies. The second run changes only this:

- every arm is generated again with the fixed generator, on the same
  demonstration and judged on the same trial cases;
- one new zero-config arm, `Z-net-probed`: the `Z-net` recording (the app's
  own traffic, no assisted read) plus `--probe-reads`. The generator sends
  `GET` probes to the local fault server right after the demonstration's save
  (the store then holds the demonstration's record, as it does for `Z-db`).
  No person writes any verifier text, so it counts toward the zero-config
  verdict;
- the first run's raw data is kept as `results-run1.json`; `results.json` and
  `generated/` hold the second run.

The kill thresholds, the baselines (`hand-written+complete` for the
thresholds), the ground truth and the trial cases are unchanged. MockLoan is
still held out: the fixes contain no MockLoan-specific code and were written
and tested against a separate local test app.

### 2026-11-09, before the third run (refuse partial probed reads)

The second run's `Z-net-probed` arm on MockLoan accepted 3 wrong runs: the
probed read covered only the written collection, and a write on another
surface passed it. The generator had labelled that file a draft, but the
protocol deletes the confirmation flags, so the file ran. Ruling: a check
that can say CONFIRMED while its read cannot see part of the save must not
be runnable. The engine change (#2129, 32997eeac) makes `scaffold-verifier`
refuse a probed read when its fault test shows a write outside the reads
would pass (`collateral_unaudited`) or a captured write no read covers. It
then writes the TODO draft and records the reason in `read_discovery.json`
and the proof. Under this protocol a refusal is "no runnable verifier".

The third run re-runs every arm on the same cases, because the harness runs
them together. Only `Z-net-probed` can change; the others' generator paths
did not change. The second run's data is kept as `results-run2.json`. The
thresholds, the baselines, the ground truth and the trial cases are
unchanged.

### 2026-11-09, after the third run (scope of the third deviation's ruling)

The ruling in the third deviation reads as if it covers every generated
check. What the engine enforces (#2129, 32997eeac) is narrower. It refuses
only a **probed** read (found by `--probe-reads`) whose fault test shows a
write outside the read would pass, or whose capture holds a write no read
covers.

A read observed in the recording's own traffic (`Z-net-read`,
`P-scaffold-verifier`) with the same open miss is still written as a draft
check. It runs under this protocol once the flags are deleted. That case is
left to a follow-up issue. The third run's numbers are unchanged by this
note.

### 2026-11-09, before the fourth run (look for a read that covers the save)

The third run refused MockLoan's probed read (`/api/disbursements`) because a
write outside it would pass. Issue #2131 changes `--probe-reads` to look for
a read that covers the whole save before it refuses. The candidates are the
sibling collections under the same prefix that the app itself read (from its
own captured `GET` requests, path only) and the prefix root (`GET <prefix>`
and `GET <prefix>/`). The probe rules are unchanged: `GET` only, the local
machine only, no credentials, no redirects, and every probe recorded in
`read_discovery.json` and the proof. The root replaces the collection read
only when it holds the written record, holds every record the collection and
each sibling returned, is one unpaged response, and every captured write went
under the prefix. The check then reads every list the root returns, and the
save may add exactly one row to it. A read that still leaves an open
`collateral_unaudited` miss is refused, as before, and the reason now says
what the search probed.

The change is app-generic (`scripts/check_engine_generality.py` passes), was
tested against separate local test apps, and adds no candidate names: the
generator does not guess store-wide routes. A generation-only smoke run
(writes nothing) on this commit found that MockLoan's app reads no other
collection under `/api`, and that `GET /api` and `GET /api/` return 404. So
the expected outcome is that `Z-net-probed` on MockLoan stays refused, with
that reason recorded. MockMed is out of scope (no JSON read route) and its
arm cannot change.

The fourth run re-runs every arm on the same cases. The third run's data is
kept as `results-run3.json`. The thresholds, the baselines, the ground truth
and the trial cases are unchanged.
