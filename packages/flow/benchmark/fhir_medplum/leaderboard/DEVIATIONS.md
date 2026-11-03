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
