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
policy updates. It carries a signature. Expiry counts updates, not hours,
because on-policy training breaks the exchangeability the bound assumes.
`is_current(policy_update)` answers whether a trainer may still use it, and
`unmet(policy)` lists every way it falls short of a contract's demands.

`RewardEvidenceReceiptV1` binds the contract digest, the policy checkpoint and
update number, the episode, the oracle tier, the evidence digest, the
component vector, the scalar, the certificate reference and its state, the
calibration corpus digest and scope, and two booleans a trainer must read:
`certified` and `development_only`.

## Synthetic scope is the only scope this version can express

The only certificate anyone can compute is against the synthetic
MockMed/ExtraDup corpus, so `calibration_scope` accepts `synthetic` and
nothing else. A `production` scope needs the Phase-1 calibration, which is not
published, so the value does not exist in the enum. It is unrepresentable
rather than merely unissued, and there is no `production_certified` property
to read. Any public text that calls a reward certified puts the word
"synthetic" next to it.

`issuer` is narrowed the same way, and for the same reason. It accepts
`self_signed` only. An `organization` issuer would assert an identity, and
this package holds no issuer key registry, so `issuer_key_id` resolves to
nothing here. Both enums keep their shape, so a later registry can add a
member without changing the field.

Two more things this version does not do, said plainly so no reader assumes
otherwise. Nothing verifies the signature: `RewardCertificateV1` checks that
`signature` is 64 base64-encoded bytes and stops there, and a consumer that
wants issuer identity verifies it against a key it already holds. And there is
no revocation list. Expiry in policy updates is the only way a certificate
stops being current.

A receipt is `certified` only when the oracle tier is 2 or 3, the certificate
is current, the calibration corpus digest is present, and the scope is stated.
The certificate also has to clear the contract that scored the episode, which
is what the next section covers.

## The contract's own certificate policy is the bar

Every `RewardContractV1` carries a `certificate_policy`: the `epsilon` and
`delta` it demands, the `threshold` the bound was calibrated at, the corpus it
was calibrated on, and the longest expiry it accepts. `score()` requires the
contract, compares the certificate against that policy, and refuses to certify
an episode when the certificate is weaker. A certificate measured at epsilon
0.248885 against a contract demanding 0.05 scores its scalar and reports
`certified` false.

`score()` also refuses a certificate whose `reward_contract_digest` names some
other contract, so a strong certificate cannot be carried across to a task it
never covered. Each refusal comes back as a sentence in
`certification_refusals`, which is empty when `certified` is true.

The receipt stores digests rather than the contract and the certificate
themselves, so it cannot check its own flag while pydantic validates it. A
reader who holds both calls
`receipt.certification_refusals(contract, certificate)` and gets the same
answer `score()` gave.

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
| 2 (API, DB, file, ack) | no | with a current certificate that clears the contract's policy |
| 3 (counterparty) | no | with a current certificate that clears the contract's policy |

The tier comes from the oracle channel, as it does for every Seal.
`refuse_development_certification` raises `RewardCertificationRefused` for
tier 0 and 1, and the receipt validator calls it. A tier-0 reward can drive a
local experiment. It cannot be certified, and a receipt that claims otherwise
does not validate.

## What a trainer receives

```python
from openadapt_types import RewardOutcomeV1, score

scalar, certified, development_only, refusals = score(
    RewardOutcomeV1.VERIFIED,
    tier=2,
    certificate=certificate,
    policy_update=120,
    contract=contract,
)
```

The contract is a required keyword. It supplies the scoring policy that turns
the outcome into `scalar`, and the certificate policy that decides `certified`,
so there is no way to score an episode without naming what it was scored
against.

Per episode, a signed `RewardEvidenceReceiptV1`: ids, digests, the tier, the
outcome, the component vector, the scalar or its absence, and the certificate
state. The trainer checks `certified` before it counts the episode toward a
certified arm and drops the episode when `scalar_reward` is `None`. When
`certified` is false, `refusals` says why.

## What stays on the organization node

The calibration corpus, the tuned adversary parameters, the checker threshold's
derivation, the oracle read recipe, and the evidence bytes. Every one of those
appears in the contract as a digest only. A consumer can prove which corpus
certified a reward. It cannot read the corpus from the receipt.

Worked contracts are in [`tests/test_reward.py`](../tests/test_reward.py).
