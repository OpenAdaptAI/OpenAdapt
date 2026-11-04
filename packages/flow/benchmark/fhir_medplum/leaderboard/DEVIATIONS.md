# Deviations from the pre-registered protocol

Every change made after the protocol was frozen (openadapt-internal
3f6fe69a, harness 7a320e98), with its reason and its effect on the scores.

## 1. Host-load guard (2026-11-02, about 22:00 EST)

**What changed:** before each run, `run.py` now waits while the host's
1-minute load average is over 35, and records the load at the start and end
of the run (`host_load` in each row). The machine's operating rule is to
keep load under 35 and to pause timed work above 40. The host load reached
about 120, mostly from other jobs on the same machine.

**Effect on scoring:** none on the definitions. The metrics, claims, oracle
and limits are unchanged. The guard can only make a run wait to start; it
never changes a run once it has started.

**Runs affected:** the 14 rows written before the guard (claude r1 P01-P06,
openai r1 P01-P08) ran while the load was high and have no `host_load`.
The protocol has no rule to rerun on load, so they are kept as they are and
flagged in the report. A slow host can only make a run more likely to reach
the 2,400-second limit, which ends it with no claim. The report states
whether any of these 14 rows did.

**Runs in flight when the processes were restarted** were not written and
started again from the beginning, the same as the protocol's rule for a
harness failure. No outcome of those runs was seen before they restarted.

## 2. Fara runs one process at a time (2026-11-02, about 22:00 EST)

**What changed:** the protocol split Fara's cases across two processes
(P01-A06 and A07-AP12) on two stacks. Because the machine's load was high,
they now run one after the other: P01-A06 first, then A07-AP12, each in
the protocol's order.

**Effect on scoring:** none. Only the wall-clock time changes.

The second process ran on the first process's stack (port 9820) instead of
its own (9830), because the host's free disk was under the lane's 30 GiB
gate for starting a stack. Each run empties and seeds the project first,
so the stack carries nothing from one run to the next. Before the second
process started, the stack's history was dumped for deviation 4's replay
(`results/fara/frozen-9820.jsonl.gz`).

## 3. Signed-out runs rerun; the session is renewed (2026-11-03, about 00:00 EST)

**What happened:** the harness signed the agent's user in once per process.
Medplum's session lasts an hour, and nothing renewed it, so every run that
finished more than an hour after its process signed in had been signed out:
the app showed its sign-in page and the agent, correctly, said NOT DONE.
That is the harness's failure, not the agent's.

**What changed:** before each run the harness signs in again if its
session is more than 15 minutes old, so a run (at most 40 minutes) always
ends inside the session. A run that starts or ends on the sign-in page now
raises a harness error, which the pre-registered rule retries up to twice.
`score.py` now also leaves out a run the harness failed three times
(`infra_note`), as PROTOCOL.md already said; before, it scored such a run
as no claim. No run had one.

**Runs affected:** every row written more than 3,600 seconds after its
process's last sign-in (the time in its `meta-*.json`) is excluded and run
again: claude 96 rows (r1 A03 onwards, all of r2 and r3), openai 84 (r1
AP03 onwards, all of r2 and r3), fara 8 (r1 P04 onwards). This rule picks
exactly the rows whose end page was the sign-in page, plus fara r1 P04,
which ran across the expiry. The excluded rows and their evidence are kept
in `results/<agent>/excluded-signed-out/` and listed in
`results/excluded-signed-out.json`; none is scored. Their spend stays in
the spend ledger and counts against each agent's share.

## 4. The verifier's token is renewed; the shadow column is replayed (2026-11-03, about 20:00 EST)

**What happened:** the harness set the verifier's read token
(`BENCH_RECORD_TOKEN`) once per process, and Medplum's tokens last an
hour. Deviation 3 renewed the browser's session but not this token. From
about an hour into each process the verifier's reads were refused (HTTP
401). Failing closed, it reported `needs_review` on 92 of 111 claude rows
and 71 of 111 openai rows without judging them, and those rows counted as
flagged in the shadow column.

**Not affected:** the end-state check and the read-back. They read with the
harness's admin token, which is renewed every 10 minutes. No row's
end-state or read-back read was refused, so the three pre-registered
metrics are unchanged.

**What changed:** `public/run.py` renews the verifier's token before its
baseline and before its check. For the 222 claude and openai runs, the
shadow column now uses the verifier's verdicts replayed from Medplum's
stored history of the same runs, with the same contracts (session
verifier-specificity, `../leaderboard_rescore/`, column "main", per-row
in `results/shadow-replay.json`). The replay reproduces all 59 live
verdicts the verifier did judge. `score.py` uses a replayed verdict where
one exists and keeps the live column (`shadow_live`) beside it.

**Fara:** its first process (P01-A06) ran before the fix, and its shadow
column is replayed the same way. Its second process (A07-AP12) runs with
the fix.
