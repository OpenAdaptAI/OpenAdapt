# Pre-registration addendum: Fara-7B's first half

Written and committed before any re-score of Fara's rows. It extends
`PREREGISTRATION.md` to a third agent. Everything there applies unchanged,
including deviations 1, 2 and 4 (the horizon, the retried seed and the review
fixes), except what this file states. The commit that adds this file fixes
the code, the contracts and the inputs below.

## Population

Fara-7B's 57 scored rows from its first half (cases P01 to A06, 3 runs each,
less the signed-out runs), on stack 9820, as committed on PR #1737 at
01854c344 and extracted to `inputs/rows-fara.jsonl.gz` (each row keeps its
source path and sha256). The server state is
`inputs/frozen-9820.jsonl.gz`, the bench project's resource history and
audit events from that stack. The run folders' times are merged into
`inputs/times.json` under `fara/`.

Fara's live checks got a fresh token at 2026-11-04 11:55 UTC. `rescore.py`
skips any Fara row checked at or after that time (`CHECKED_BEFORE`), since
its live verdict stands. Every row in this population was checked before it.

## Columns

The same three as the main analysis: **main**, **operations** (v1,
`between: consistent`) and **operations-v2** (`between: either`). Here
**operations-v2** is registered, not exploratory: its rule was fixed before
any Fara verdict was computed. The primary comparison stays **operations**.

## Fidelity gate

Four Fara rows (r1-P01 to r1-P04) were judged live (no `HTTP 401`). The
**main** column must equal them as defined in `PREREGISTRATION.md`.

## What was seen before registering

- The oracle labels of all 57 rows.
- Which rows the live verifier judged, and the live verdict of r1-P01
  (`needs_review`, no record and no agent write), seen while checking the
  row format.
- No re-scored verdict of any Fara row, under any contract.

## Commands

```bash
for c in main operations operations-v2; do
  python rescore.py --agent fara --contracts $c --out results/rescore-$c-fara.json
done
python score_rescore.py --agent fara results/rescore-{main,operations,operations-v2}-fara.json \
  --out results/score-fara.json
```

The claude and openai results in `results/` are not re-run. Their files'
`inputs` field lists the inputs as they were then; `RESULTS-SHA256SUMS`
still pins what produced them.
