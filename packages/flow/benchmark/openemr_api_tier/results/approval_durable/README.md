# OpenEMR referral: API step under per-item approval and a durable run

**Label: API step under each_item approval + durable, no run gate; harness
answers approvals; approval wait excluded.** These are not numbers for the
governed production profile. The referral bundle doesn't pass the run gate
yet: it is refused on its GUI path, for reasons unrelated to the API step
(see the admission list in the session notes, and the local-edit proposal,
PR #1366).

Harness: [`approval_durable.py`](../../approval_durable.py). Data:
[`2026-10-19-combined/`](2026-10-19-combined/) (`runs.csv`, `summary.json`,
and each item's `result.json` under `items/`).

## What ran

- The OpenEMR new-patient referral, compiled from a recording with network
  capture and admitted with `--identity subject_name=lname`, on a local
  OpenEMR test copy with synthetic patients only.
- Two arms with the same bundle and record check: API steps off (`browser`)
  and on (`api`). Both use `ApprovalGate("each_item")` and `durable=True`.
- 5 blocks of 4 referrals per arm, interleaved, with the arm order alternating
  by block. Each block resets the fixture and seeds the existing chart that
  referral r6 must not duplicate.
- The harness answers each approval request as soon as it is written,
  standing in for the person. The time spent waiting for that answer is
  measured (`approval_wait_s`) and left out of `engine_s`.
- Correctness is checked by the test setup's own direct SQL read-back. A new
  referral is correct when exactly one new chart exists and it matches all 12
  fields. r6 is correct when no chart is created.
- Each item waited until the machine's 1-minute load was under 12. The run
  was paused for a shared quiet window after 19 of 40 runs and resumed into a
  second directory. The interrupted item was run again from the start. Load
  per item was not kept for the rebuilt rows.
- Engine: the API-step stack (PRs #1358, #1361, #1362, #1364) at `352af585b`.
  It does not include the review fixes #1367–#1372. With #1370 and #1371 this
  referral's API step is refused: OpenEMR sends a background write while the
  form is filled, and its form runs script on submit and on the name fields.

## Results

| Arm | Runs | Correct by read-back | Saved through | Median engine time | Approvals per item |
| --- | --- | --- | --- | --- | --- |
| API steps off | 20 | 19, plus 1 safe stop (nothing saved) | GUI | 15.95 s | 2 (1 for r6) |
| API step | 20 | 20 | API 18, GUI 2 (r6) | 1.12 s | 1 |

- **API step.** Each new referral sent one request the person approved, with
  the operation digest bound to the request bytes, and read back 12 of 12.
  For r6 the API step's duplicate pre-check found the existing chart, so the
  API step was not used. The GUI steps ran instead and created nothing, as
  they should.
- **API steps off.** The GUI asks twice per new referral: once at "Create New
  Patient", and once at the confirming save. One item (b01, r2) stopped itself
  at the save's identity check (coverage 0.81) and saved nothing. The
  earlier spike saw the same fail-closed stop once in 20 runs.
- No run saved a wrong or duplicate chart.
