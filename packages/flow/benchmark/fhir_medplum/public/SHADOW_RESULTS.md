# Shadow mode on live Medplum

Shadow mode (`openadapt shadow`, [SHADOW_MODE.md](../../../docs/SHADOW_MODE.md))
ran beside two agents on the 37 cases of this benchmark, on 2026-10-31. The
agent wrote through the shadow, then through the fault proxy, then to
Medplum 5.2.8:

```
agent -> shadow (checks, never blocks) -> fault proxy (breaks writes) -> Medplum
```

So the shadow saw what the agent sent, and Medplum held what the faults made
of it. The end-state oracle (`oracle.py`) decided what really happened in
each case. Runner: `shadow_run.py`. Data: `results/shadow/`.

## What it caught

| | Scripted agent | Claude Sonnet 5.5 (Agent SDK) |
|---|---:|---:|
| Harmful end states | 18 | 9 |
| Caught at the check (REFUTED) | 17 | 8 |
| Caught on the recheck (REFUTED later) | 1 | 1 |
| Missed | 0 | 0 |
| Correct writes CONFIRMED (check and recheck) | 13 | 18 |
| Correct writes REFUTED | 0 | 4 |
| Writes that never landed: UNCERTAIN | 2 | 1 |
| Writes seen, in checks | 33 in 33 | 42 in 32 |

- **The recheck** caught A10 for both agents. In A10, the fault proxy adds a
  second allergy with the agent's own credential after the agent has
  answered and the first check has passed. The recheck runs over the window
  since the first check. In the benchmark it runs as soon as the case ends;
  the command's default is 300 s.
- **Claude's 4 correct writes REFUTED** are the benchmark's own 4 hard
  cases: A04, A05, AP03 and AP11. In each, the fault proxy saved the record
  as a different patient, substance or slot, and Claude fixed it with one PUT.
  The end state is right. The record was the wrong one for a while, so the
  verifier sends it to a person. The report files it under "saved as another
  record first, then corrected". `README.md` describes the same 4 cases for
  the benchmark's own verifier arm.
- **UNCERTAIN, never CONFIRMED,** for a write that didn't land. P11: the
  server refused it. P13: a reply said "created" and nothing was saved; the
  Claude agent retried P11 and its retry landed.

How they landed wrong, as the report groups them (scripted agent): a value
differs from what the agent sent (9), another record changed (4), saved more
than once (3), holds a value the agent didn't send (1).

## It adds no writes and doesn't change them

- Medplum's audit trail shows **0** creates, updates or deletes by the
  shadow's read credential in either run.
- In every case, the fault proxy received exactly as many writes as the agent
  sent to the shadow.
- `tests/test_shadow.py` checks that the bytes the app receives are the
  bytes the agent sent, and that a shadow failure still sends the write on.

## Overhead

The only delay is the read before a write's first request. It holds the
write until the store has been read, for at most `hold_ms` (2,000 ms).

| | Median | Largest |
|---|---:|---:|
| Scripted agent, added per write | 15.2 ms | 37.8 ms |
| Claude agent, added per write | 25.3 ms | 45.0 ms |

Medplum ran on loopback in a Podman VM on the same Mac.

## Setup time (the B4 bar: at most 60 minutes, at most 3 decisions)

`shadow_setup_timing.py` ran each step a vendor takes against the running
Medplum, and timed it (`results/shadow/setup/setup-timing.json`):

| Step | Time |
|---|---:|
| 1. A read credential for the shadow (a new client application) | 0.1 s |
| 2. The agent's actor reference (`/auth/me`; Medplum's sign-in took 61 s) | 61.1 s |
| 3. `shadow.yaml` (5 lines) and the three reference contracts, copied | 0.0 s |
| 4. `openadapt shadow --config shadow.yaml` started | 0.6 s |
| 5. The agent's first task, its check, its recheck and the report | 14.6 s |
| **Machine time, from nothing to a confirmed and rechecked first write** | **76.5 s** |

It took three decisions:

1. which server the agent writes to;
2. which contracts (the reference ones, used as they are);
3. the shadow's read credential and the agent's actor reference.

Installing took 1 s from a warm cache. The benchmark's own cold install took
under a minute (`README.md`).

A script can't time a person reading the guide, creating a client in
Medplum's admin app, or deciding. No person was timed. The bar's 60 minutes
leaves about 57 for those three decisions. This measures the steps, not a
first-time vendor.

In the first timing run the shadow flagged a real duplicate. The scripted
agent searches by family name only. A person an earlier timing run had
registered (same email, phone and birth date, a different family name) was
created again, and the shadow reported "1 other record(s) are now also the
person". The timing task now uses a new person each run.

## What it doesn't cover

- **Values the agent was told.** The shadow checks that the store holds
  what the agent sent. It doesn't know the task, so it can't catch an agent
  that sends the wrong value itself.
- **Updates and deletes of existing records.** These are UNCERTAIN: an
  ensure-record contract checks creates.
- **Changes after the recheck's read.**

## Rerun

Medplum on ports 9740 and 9741, the shadow on 9742, the proxy on 9743:

```bash
python -m benchmark.fhir_medplum.public.shadow_run --agent scripted --port 9740
```

Add `--agent claude` with `ANTHROPIC_API_KEY` set; the Claude run's model
spend was $0.72. After these runs, the kind labels in both reports were re-derived
with the final `KINDS` table in `shadow_report.py`; the verdicts are as run.
