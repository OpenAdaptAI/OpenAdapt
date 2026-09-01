# Reward contracts

A trainer that optimizes against a checker inherits the checker's errors. The
reward contract says what earns reward, who reads the terminal effect, and
what bound that reader carries. The receipt says one thing: OpenAdapt verified
the terminal effect of one episode. It does not say that Flow governed the
policy's actions.

This is not an Execute Seal. A model rollout never receives
`ExecuteEvidenceReceiptV1`. A production Flow result requires a qualified
deterministic program with zero model use. The reward receipt has its own
schema id, `openadapt.reward-evidence-receipt/v1`, and none of the Seal's
fields (`execution_id`, `workflow_digest`, `qualification_id`, `contracts`), so
the two cannot be swapped.

## The three contracts

`RewardContractV1` binds a task and environment by opaque id and digest, the
required and forbidden effect contracts by digest, the independent oracle
(channel plus identity keys), the reward components and their weights, the
scoring policy, and the certificate policy. Its `digest` is the canonical
SHA-256 over sorted JSON. Components sort by name, so two authors who list them
in a different order get the same digest.

`RewardCertificateV1` is the bound: `epsilon`, `delta`, `threshold`, the
calibration corpus digest, the calibration scope, the checker configuration
digest, the issuer, the policy update it was issued at, and its expiry in
policy updates. It is signed. Expiry counts updates, not hours, because
on-policy training breaks the exchangeability the bound assumes.
`is_current(policy_update)` answers whether a trainer may still use it.

`RewardEvidenceReceiptV1` binds the contract digest, the policy checkpoint and
update number, the episode, the oracle tier, the evidence digest, the
component vector, the scalar, the certificate reference and its state, the
calibration corpus digest and scope, and two booleans a trainer must read:
`certified` and `development_only`.

## Synthetic scope is the only scope today

Today the only certificate anyone can compute is against the synthetic
MockMed/ExtraDup corpus, so its `calibration_scope` is `synthetic`. A
`production` scope needs the Phase-1 calibration, which is not published.
Until that changes, the word "certified" in any public text about a reward
must sit next to the word "synthetic".

The types hold that line. A certificate with `issuer: self_signed` may carry
only `synthetic` scope; `self_signed` plus `production` does not validate. A
receipt is `certified` only when the oracle tier is 2 or 3, the certificate is
current, the calibration corpus digest is present, and the scope is stated.
`production_certified` is true only when that scope is `production`, which no
self-signed certificate can reach.

## Outcome to scalar

| `reward_outcome` | Class | Scalar |
| --- | --- | --- |
| `verified` | admitted positive | `verified_reward` (default 1.0) |
| `halted_before_effect` | zero or penalty | `halted_before_effect_reward` (default 0.0) |
| `refused` | zero or penalty | `refused_reward` (default 0.0) |
| `rejected_policy` | zero or penalty | `rejected_policy_reward` (default 0.0) |
| `wrong_effect` | zero or penalty | `wrong_effect_reward` (default -1.0) |
| `reconciliation_required` | unscored | none |
| `failed_platform` | unscored | none |

An unscored episode has no scalar. `score()` returns `None`, the receipt
refuses a `scalar_reward`, and the contract cannot declare
`uncertain_episodes` or `platform_failures` as anything but `unscored`. A
trainer that folds those into 0.0 is training on platform noise, which is the
failure this contract exists to stop.

## Tier to certification

| Oracle tier | `development_only` | `certified` |
| --- | --- | --- |
| 0 (visual, OCR) | yes | never |
| 1 (second session) | yes | never |
| 2 (API, DB, file, ack) | no | with a current certificate, corpus digest, and stated scope |
| 3 (counterparty) | no | with a current certificate, corpus digest, and stated scope |

The tier comes from the oracle channel, as it does for every Seal.
`refuse_development_certification` raises `RewardCertificationRefused` for
tier 0 and 1, and the receipt validator calls it. A tier-0 reward can drive a
local experiment. It cannot be certified, and a receipt that claims otherwise
does not validate.

## What a trainer receives

```python
from openadapt_types import RewardOutcomeV1, score

scalar, certified, development_only = score(
    RewardOutcomeV1.VERIFIED,
    tier=2,
    certificate=certificate,
    policy_update=120,
)
```

Per episode, a signed `RewardEvidenceReceiptV1`: ids, digests, the tier, the
outcome, the component vector, the scalar or its absence, and the certificate
state. The trainer checks `certified` before it counts the episode toward a
certified arm and drops the episode when `scalar_reward` is `None`.

## What stays on the organization node

The calibration corpus, the tuned adversary parameters, the checker threshold's
derivation, the oracle read recipe, and the evidence bytes. Every one of those
appears in the contract as a digest only. A consumer can prove which corpus
certified a reward. It cannot read the corpus from the receipt.

Worked contracts are in [`tests/test_reward.py`](../tests/test_reward.py).
