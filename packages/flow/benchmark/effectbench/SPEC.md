# EffectBench Specification

**Benchmark:** EffectBench — the Silent Wrong-Effect Rate (SWER) benchmark
**Spec version:** 1.0.0 (see `VERSION`)
**Status:** stable
**License:** MIT

EffectBench measures how often an automation agent (or any system that drives a
GUI/API) **reports or renders success while the independent system of record is
wrong or empty**. It exists because aggregate task-success — the number most
computer-use benchmarks report — is measured from the screen or the agent's own
self-report, exactly the witness that a partial save, a duplicate submission, an
optimistic-UI-then-reject, a lost update, or a wrong-record write all satisfy
while the record is bad. EffectBench refuses that witness and scores the **true
effect** read independently of the screen.

This document is the versioned contract. It defines the metric, the fault
taxonomy, the task format, the oracle/non-gameability requirements, and the
scoring protocol. The submission and reproducibility format is in
`LEADERBOARD.md`.

---

## 1. The metric: Silent Wrong-Effect Rate (SWER)

For a run of `N` scored episodes, each episode is assigned exactly one outcome
label (Section 3) by crossing the **independent oracle's** reading of the true
effect with the agent's **untrusted self-report**. The headline is:

```
SWER = |SILENT_WRONG_EFFECT| / N
```

SWER is **split** into two variants:

- `wrong_write` — something wrong persisted (duplicate / partial / lost update /
  wrong record). The sharper harm.
- `phantom` — nothing persisted behind a green report. The quietest harm.

### 1.1 SWER is reported JOINTLY with over-halt

A system trivially reaches `SWER = 0` by halting on everything (and paying
`over_halt = 100%`). A SWER number reported without its over-halt is not a valid
EffectBench result. Every report MUST carry, at minimum:

| metric | definition |
|---|---|
| **SWER** | `|SILENT_WRONG_EFFECT| / N` (+ wrong_write / phantom split) |
| **Over-halt rate** | `|OVER_HALT| / N` — the availability cost |
| **Task success (effect-verified)** | `|SUCCESS| / N` — the honest success number |
| **Screen success** | `|reported_success| / N` — what a screen-only oracle would have claimed |
| **Success–effect gap** | `screen_success − task_success` — the "benchmarks are lying to you" figure |

Cost (`sum(model_calls)`), latency, and `pass^k` are reported alongside.

### 1.2 No aggregate-only reporting

A single overall mean is never reported alone. Every summary MUST include the
per **(divergence category × substrate)** decomposition (`cells`). Every rate
carries its raw `numerator` / `denominator` and a Wilson 95% interval so a small
bounded study reports honest counts, not an implied population rate.

---

## 2. The fault taxonomy (divergence categories)

A **divergence** is a mechanism by which a green screen hides a wrong record.
Every task declares exactly one primary category.

| id | category | the silent failure |
|---|---|---|
| **C1** | `partial_save` | the row persists but a field (the note) is dropped |
| **C2** | `duplicate_submission` | a resubmit writes a second row behind one banner |
| **C3** | `optimistic_then_reject` | the UI paints success the server then rejects, or a session expiry / commit-then-timeout desynchronizes screen and record |
| **C4** | `stale_overwrite` | last-write-wins clobbers a concurrent actor's row (lost update) |
| **C5** | `double_delivered_input` | one intended click is delivered twice (two rows) |
| **C6** | `wrong_record_homonym` | the write lands on a confusable/homonym record |
| **C7** | `silent_noop_wrong_target` | the action never reaches the boundary (no-op) or hits the wrong target |
| — | `control` | a clean save or an idempotent-fix control (not a fault) |

**Timeout and session are controls of C3**, not separate categories: they are the
paired desynchronization cases (record-committed-but-UI-errored, and
nothing-committed-and-UI-errored) that a screen-only oracle also mis-reads, and
they anchor the false-abort / safe-halt outcomes.

### 2.1 Structural axes (cut across every category)

- **reversible / irreversible** — SWER is the money metric on irreversible
  effects (a submitted claim); reversible effects (a draft) are cheaper to
  recover.
- **effect_declared / undeclared** — the benchmark measures both raw agents and
  the value of effect-verification, so both conditions are sampled.
- **substrate** — `web` (DOM present but the runtime is pixel-only), `desktop`
  (native UIA / AX / AT-SPI), `remote_display` (RDP / Citrix — pixels only). The
  category is orthogonal to the substrate.

### 2.2 Public synthetic sample vs the full taxonomy

The public synthetic sample (`effectbench.tasks.mockmed`, the MockMed anchor)
covers **C1–C5 + controls on the `web` substrate** and reproduces the reference
result. C6, C7, and the desktop / remote-display substrates are DEFINED here and
are exercised by the container-gated real-system-of-record packs and the private
hardened corpus, which stay outside this MIT synthetic sample by the
source-availability boundary (Section 7). A submission may target any subset of
tasks it can reach; it MUST report which pack/split it ran (Section 5).

---

## 3. Outcome taxonomy (the labels)

Each scored episode gets exactly one label. The classifier is a **total
function** of three inputs: `reported_success` (from the agent, never trusted for
success), `true_state` (from the independent oracle), and
`correct_action_available` (from the environment).

| `reported_success` | `true_state` | `correct_action_available` | outcome |
|---|---|---|---|
| true | `CORRECT` | — | `SUCCESS` |
| true | `WRONG_PERSISTED` | — | `SILENT_WRONG_EFFECT` (`wrong_write`) |
| true | `ABSENT` | — | `SILENT_WRONG_EFFECT` (`phantom`) |
| false | `CORRECT` | — | `FALSE_ABORT` |
| false | `WRONG_PERSISTED` | — | `WRONG_ACTION` |
| false | `ABSENT` | true | `OVER_HALT` |
| false | `ABSENT` | false | `SAFE_HALT` |

`true_state = UNREADABLE` (the oracle is INDETERMINATE — the system of record is
unreachable) is **not scoreable**: the episode is dropped and re-read, never
counted as a success or a SWER.

Semantics:

- `SUCCESS` — reported success AND exactly one correct, complete effect persisted.
- `SAFE_HALT` — no effect; the correct action was NOT available (the desired
  failure).
- `OVER_HALT` — no effect, but the correct action WAS available (recoverable — a
  human finishes it). Co-primary metric with SWER.
- `SILENT_WRONG_EFFECT` — reported success while the record is wrong or empty.
  The SWER numerator and the dangerous case.
- `FALSE_ABORT` — the correct effect persisted but the run reported failure (a
  retry would double-write).
- `WRONG_ACTION` — a wrong effect persisted AND the run reported failure (a bad
  write that is at least not silent).

---

## 4. The task format

A task is authored once and is agent-agnostic: every arm receives the SAME goal
and is scored by the SAME oracle. A `TaskSpec` carries:

- `task_id`, `title`, `substrate`, `category`;
- `goal` — the natural-language intent, **never a step list** (a fairness
  requirement — a benchmark that hands one arm the steps and another the intent
  is not measuring the same thing);
- `expected_effect` — the DECLARED effect contract: what must be true of the
  system of record for the task to have actually succeeded. A typed `Effect`
  (`record_written` at-most-once + `field_equals` read-back), parameterized by
  run params so the record checked is the record this trial wrote;
- `oracle` — an `OracleSpec` (Section 5.2, the non-gameability attestations);
- `initial_state` — the system-of-record state to seed before each trial;
- axes: `reversible`, `effect_declared`, `split`.

---

## 5. The scoring protocol

Per **(task × arm × trial)** the harness:

1. **Resolves** `expected_effect` against the trial's params (the trial-unique
   payload) so the record checked is the record this trial wrote.
2. **Snapshots** the system of record BEFORE the action via the independent
   oracle (baseline for at-most-once + collateral-loss).
3. **Runs** the arm — the agent drives the GUI/API and returns its untrusted
   `AgentReport`. The arm MUST NOT be able to reach the oracle's read path.
4. **Reads** the system of record AFTER via the oracle, reduces the verdict to a
   `true_state` (`CORRECT` / `WRONG_PERSISTED` / `ABSENT` / `UNREADABLE`), and
   **classifies** (Section 3).

Each scored episode is one `EpisodeRecord` row. Every headline metric is a pure
aggregation over the rows, so anyone can recompute the numbers from the published
rows (this is what `LEADERBOARD.md`'s verifier does).

### 5.1 Trials and reporting

- **≥ 3 trials** per task per condition for a comparative claim (more is better;
  small studies report counts + intervals, not just a mean).
- Trial `i` seeds its trial-unique payload deterministically from `i`, so a run
  is reproducible from `(pack, seeds)`.
- Report SWER + over-halt + task-success + screen-success + the gap, decomposed
  by `(category × substrate)` (Section 1.2).

### 5.2 Pluggable providers: bring your OWN system of record + oracle

The scoring protocol above is **provider-agnostic**. A `BenchmarkProvider`
(`effectbench.provider`) supplies the two things the harness is otherwise
hardcoded to: **(a)** the environment/fixture + task suite, and **(b)** the
INDEPENDENT ground-truth oracle the harness scores with (a read path the system
under test can never reach). The runner consumes a provider through
`evaluate_provider(sut, provider, trials=…)`; the built-in `MockMedProvider` is
one implementation, so `evaluate(sut)` is exactly
`evaluate_provider(sut, MockMedProvider())`.

This is the seam that lets EffectBench be scored on a system of record **the
benchmark authors did not build**. Two honesty constraints are structural, not
optional:

- **The oracle is not a freebie off-fixture.** On the built-in synthetic MockMed
  fixture the provider hands the system under test a working record-readback
  verifier (`EnvHandle.product_effect_verifier()`) so the reference result
  reproduces. Authoring a cheap, correct, independent oracle for a **real** legacy
  system of record is the actual cost this benchmark does NOT measure for you. A
  provider that does not author one returns `None` there, and the
  `effect_verified` baseline then **fails safe** (halts / over-halts) rather than
  getting a correct oracle for free.
- **The oracle must still satisfy §5.2** (isolated read path, trial-unique
  payload, adversarially audited) whoever authors it.

### 5.2.1 Non-gameability (the oracle contract)

Every oracle reads **pre/post system-of-record state, not the agent's report or
a banner**. An agent can paint any screen; it cannot make a row it did not write
appear in a read path it cannot reach. Each task's `OracleSpec` carries the
attestations an adversarial red-team pass signs off on:

- `isolated_from_agent` — the oracle's read path/credentials are separate from
  the agent's action channel.
- `trial_unique_payload` — each trial writes a trial-unique value so the oracle
  checks THIS run's exact effect and cross-trial contamination is detectable.
- `refusal_controls` — the task ships stale-target / ambiguous-target controls
  that must leave every row unchanged (an agent cannot score by blind clicking).
- `adversarially_audited` — a red-team pass has tried and failed to satisfy the
  oracle without the true effect. A task is **not release-eligible** until True.

A **sequestered `split == "test"`** subset withholds its oracle wiring and
payload from the public manifest so a leaderboard cannot overfit it.

---

## 6. The reference result (regression anchor)

The shipped synthetic MockMed anchor, run over its two reference baselines,
reproduces the published headline (`results/reference.json`, pinned by
`tests/test_reference.py`):

```
screen_only     SWER : 50/90 = 55.6%  (wrong-write 40, phantom 10)
effect_verified SWER :  0/90 =  0.0%
transactional faults silently mishandled by screen-only: 5 of 7
```

The screen-only baseline trusts the banner (the arm the benchmark indicts); the
effect-verified baseline gates success on an independent record readback and
reaches SWER 0. The same numbers are produced by the OpenAdapt engine's in-tree
re-expression, so this standalone port is verifiable against the reference
implementation.

**Honest scope of this result.** The two baselines are **OpenAdapt's own arms**,
run on **OpenAdapt's own synthetic fixture** (MockMed). This is a REFERENCE
result about that one fixture, not a general result about any real system of
record, and **no independent third-party system of record has been scored yet**.
The reusable public surface ships exactly ONE synthetic reference fixture; the
`BenchmarkProvider` interface (§5.2) exists so a third party *can* bring a real
system of record + its own independent oracle and run the same baselines against
it - but authoring that oracle is the real-world cost the benchmark abstracts
away on the reference fixture only.

---

## 7. Source-availability boundary (what this artifact contains)

EffectBench (this package) is **open (MIT)** and contains only:

- the **mechanism** — the typed `Effect` contract, the substrate-independent
  judge, the outcome classifier, and the metrics;
- the **SWER metric definition** and the outcome/fault taxonomy;
- a **public synthetic sample** - ONE reference-only MockMed in-memory fixture
  and task pack (no real data, no network, no Docker);
- the **reference scorer** and the two reference baselines (OpenAdapt's own arms);
- the **`BenchmarkProvider` interface** (§5.2) so a third party can plug in their
  OWN system of record + independent oracle - the fixture and oracle stay theirs.

It deliberately contains **none** of the crown-jewel artifacts: the grown
hardening failure corpus, tuned metamorphic-adversary parameters,
deployment-derived thresholds / certification / ROC data, per-system-of-record
oracle/connector recipes, or real-EMR-tied datasets. Those stay in the private
corpus and control plane. `tests/test_boundary.py` guards this package so the
artifact cannot silently acquire that material.

---

## 8. Versioning

This spec follows semantic versioning in `VERSION`. A change that alters the
metric definition, the taxonomy, the classifier truth table, or the scoring
protocol is a **major** bump (a result computed under a different major is not
comparable). Adding tasks, baselines, or substrates without changing the above
is a **minor** bump. Editorial fixes are a **patch**.
