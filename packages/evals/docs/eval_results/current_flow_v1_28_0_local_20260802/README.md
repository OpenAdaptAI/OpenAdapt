# Flow 1.28.0 local evidence (2026-08-02)

Measured against the exact published `openadapt-flow` 1.28.0 wheel, SHA-256
`4d156035ea411e3cbdbc40978d653d50727a7d8664646be62f7f9e95ba0c7202`, bound to
release-tagged tracked-clean source `b646276a086c74b65ba850cdef2e475ca53f10c0`.
Zero model calls, $0.00 model cost, loopback only.

This is a retained `stale` entry in `docs/eval_results/PUBLISHED_EVIDENCE.json`.
Flow 1.30.0 is current, and this run did not retain an exact browser revision or
installed dependency snapshot. Do not relabel these results. A governed 1.30
rerun must replace them. This set superseded
`current_flow_v1_27_1_local_20260802`, which remains committed and reproducible
against the 1.27.1 wheel it measured.

| File | What it holds |
|---|---|
| `REPORT.md`, `results.json` | Compiled replay versus steelmanned DOM selector controls: 3 conditions x 3 arms x 3 trials, no retries |
| `COMPARISON_TO_v1_27_1.md` | Release-over-release delta, including the one number that moved and the safety properties of the remote frame-lease change |
| `REPRODUCE.md` | Exact commands, digests, and environment capture |
| `clean_postcondition_over_halt.json` | Zero observations — the 1.24.0 `clean` over-halt stays fixed |
| `theme_postcondition_over_halt.json` | Zero observations — the 1.16.1 `theme` over-halt stays fixed |
| `replication/` | An independent re-run of the whole comparison |
| `transaction_probe/` | Transaction outcome taxonomy against MockMed's persistence boundary: 5 fault modes x 2 verification configurations x 3 trials |
| `remote_lease_safety/` | The two safety properties of Flow 1.28.0's remote frame-lease delivery: 3 cells x 3 trials, each classified under all 3 execution profiles |

There is no `transaction_probe/invariant_violations.json` in this set. The
runner writes that file only when an invariant is violated, and none was.

## The comparison is controlled

Both harness files are byte-identical to the 1.27.1 measurement — comparison
runner `68f9e5a2…8a126`, probe runner `097cc900…40ce8` — and the bundled MockMed
tree is byte-identical at `f0736c7a`. This is the first release-over-release
comparison in this directory where neither the fixture nor either harness moved,
so no delta can be a measurement artefact.

## Headline results

**Held, and nothing moved.** Compiled replay completed the task 3/3 in every
condition, in the primary run and in its independent replication — 18 counted
compiled trials with zero over-halts, zero silent incorrect successes, zero
wrong-target writes, zero model calls, and $0.00. The DOM selector controls
still fail 0/3 under label drift, loudly, at the first renamed locator and
before any mutation. Identity coverage is unchanged at 5 of 8 applicable clicks
armed. Every one of the 10 transaction-probe cells reports the same outcome as
on 1.27.1, with 0 violations across 30 counted runs.

**Worse: compiled replay is slower.** The compiled steady-state median rose in
all 6 condition-by-run cells, by +0.099s to +0.252s (+1.4% to +3.6%). In the
`clean` condition the two 6-trial samples do not overlap. The harness and the
fixture were identical, so this is not a measurement artefact; but the two
evidence sets were measured in separate sessions rather than interleaved, so
this design cannot separate an engine effect from host drift, and the delta is
recorded as unattributed. It changes no outcome classification. See
`COMPARISON_TO_v1_27_1.md`.

**Measured, not trusted: the remote frame-lease change.** `c9618cc` lets a
consequential remote click be delivered through a backend's one-shot actuation
lease when no typed delivery receipt exists. Both stated safety properties hold
under measurement. A governed run still refused in 3 of 3 runs, before the first
input edge, with zero input edges reaching the backend. And across 9 counted
runs classified under all 3 execution profiles — 27 classifications —
`VERIFIED` was returned 0 times, `production_eligible` was true 0 times, and
`transaction_billable` was true 0 times; every lease delivery classified as
`COMPLETED_UNVERIFIED`. A frame that changed between the lease and the input
edge aborted delivery in 3 of 3 runs.

**Not claimed.** No Flow-versus-zero-shot comparison was run; it needs a paid
model and a paid WAA environment, and the live replay path still lacks a wired
WAA evaluator. Two transaction invariants — "an outcome asserting no business
effect must not be reported when the record shows the write landed" and
"`VERIFIED` must never be reported when nothing landed" — are reported as
`vacuous` rather than as passes, because no counted run could exercise them. A
vacuous invariant proves nothing. The lease-safety probe uses a fake backend
exposing only the two-phase lease; it establishes a runtime contract and says
nothing about a real Citrix or RDP session.
