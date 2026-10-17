# Paid computer-use agent arm across three reference verticals

Status: **small-N local engineering evidence, not publication evidence**.

On 2026-07-21, a paid `claude-sonnet-5` computer-use agent was evaluated on
three pinned, synthetic reference environments: Frappe Lending, openIMIS, and
the pinned-local OpenEMR subset. Each trial began from a reset baseline, drove
the same task contract used by the compiled reference, and was classified by
an arm-independent system-of-record oracle. Pixels and the agent's self-report
did not establish success.

## Aggregate results

| Vertical | Environment | Trials | Primary outcome | Silent incorrect success | Mean list-price model cost | Mean wall time |
|---|---|---:|---|---:|---:|---:|
| Insurance | openIMIS | 3 | **3/3 correct** | 0/3 | $0.4793/run | 69.8 s |
| Lending | Frappe Lending | 6 | **6/6 correct writes; 5/6 clean** (one post-write cost-cap over-halt) | 0/6 | $0.4240/run | 67.3 s |
| Healthcare | OpenEMR (local) | 6 | **0/6 correct; 6/6 missing write** | 0/6 | $0.8901/run | 113.3 s |

Across these 15 trials, the agent recorded zero silent incorrect successes. The
only classified over-halt was the one post-write lending cost-cap halt; the
insurance and OpenEMR arms recorded 0/3 and 0/6 over-halts, respectively. The
OpenEMR result is a negative result: the bounded agent exhausted its action
budget without completing the write in every trial.

## Method and public/private boundary

The public, generic agent-baseline mechanism remains in
`openadapt_flow/benchmark/agent_baseline.py`. It provides explicit paid-run
opt-in, action and cost caps, usage accounting, and an agent self-report that
is never trusted for scoring. The public benchmark schemas and aggregate
failure taxonomy also remain available for independently authored tasks.

The application-specific driver adapters and oracle wiring, raw per-run
JSON/JSONL, environment fingerprints, screenshots, and detailed cost ledger are
private evidence. They are retained with file-level provenance and hashes in
the private `OpenAdaptAI/openadapt-corpus` repository and are intentionally
excluded from this public repository and its distributions.

## Caveats

- These agent trials used freshly provisioned baselines separate from the
  earlier compiled/API model-free subsets. The columns must not be presented as
  a matched three-arm comparison.
- The sample is small: 3 trials for insurance and 6 each for lending and
  healthcare.
- The environments used synthetic data on one local host. This is not customer,
  regulated-production, clean-machine, or broad reliability evidence.
- A publication comparison still requires a preregistered, matched protocol
  with at least 10 fresh trials per task and condition, a common baseline,
  independent review, and complete reporting of incorrect success and
  over-halt.

This aggregate report does not claim publication readiness, certification,
commercial availability, or superiority over computer-use agents generally.
