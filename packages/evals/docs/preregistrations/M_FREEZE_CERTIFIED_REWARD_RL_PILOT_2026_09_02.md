# M-freeze: certified-reward RL Phase-1/pilot (2026-09-02)

This is the M-freeze amendment named in section 3.5 of
[`PREREGISTRATION_CERTIFIED_REWARD_RL_2026_08_25.md`](PREREGISTRATION_CERTIFIED_REWARD_RL_2026_08_25.md).
The machine-readable freeze is the sibling JSON. If the two disagree, the JSON wins.

It exists so Phase-1/pilot training can't shop seeds. It does not report a
training result. No training step that this amendment governs may start
before this file is committed.

## What it pins

- Base checkpoint: `Qwen/Qwen2.5-VL-3B-Instruct` at Hugging Face revision
  `66285546d2b821cf421d4f5eb2576359d3770cd3`.
- Learning rate `1.0e-6`. GRPO group size `4`.
- Seed schedule K=3: `101`, `202`, `303`. Same schedule as
  `openadapt_evals.reward.proof.DEFAULT_SEEDS`. Every arm times every seed
  reports. No best-seed pick, no replacement seed.
- Certificate digest, from the committed synthetic-scope proof:
  `sha256:7e98d24947d6511a2e354f2f1d0e656e3a1604ac57331439f6bc3a96c2995be9`
  in `docs/reward/proof_2026-09-01.json`. ε = 0.181036, δ = 0.05,
  expiry C = 100 policy updates. Synthetic scope only.
- ExtraDup mutants: `dup`, `extra`, `omit`, `unsubmit`, `claim`. They stay
  out of the training dataset and out of the training reward.
- Train patient ids `p1`..`p8`. Holdout patient ids `h1`..`h8`. Disjoint.
  Train operators are `control` only.
- Arms: `visual_only`, `certified_sor`, `shuffled`, `no-train`.
- Primary metric: holdout silent incorrect success, scored by the
  independent ExtraDup SoR checker, never by the training reward.
- Secondary metrics: honest-write success, over-halt, group-unscored rate.

`execute_seal` stays false. This freeze does not mint a Seal and does not
set `production_acceptance`.

## How to check it

```text
python -m pytest tests/test_m_freeze_certified_reward.py
shasum -a 256 docs/preregistrations/M_FREEZE_CERTIFIED_REWARD_RL_PILOT_2026_09_02.json
```

A later change to a pinned source (ExtraDup operators, the proof
certificate, the seed schedule) needs a new dated amendment. It does not
edit this one in place.
