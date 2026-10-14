# Training against a certified reward

`openadapt_evals.reward` wires a TRL GRPO trainer or a verl trainer to a reward
endpoint that answers with a signed `RewardEvidenceReceiptV1` from
`openadapt-types`. The receipt says what an independent oracle read at the end
of one episode, at what tier, and under which certificate. The adapters turn
that into the scalar the trainer expects and refuse the three shortcuts that
make a reward lie: scoring an unreadable outcome as 0, calling a tier-0 read
certified, and training past a certificate's expiry.

The reward worker (oracle read, judge, signing, the HTTP route) lives in
`openadapt-flow`. This package only consumes what it emits. For tests and the
proof below, `openadapt_evals.reward.devsigner` builds receipts from the same
contract with a throwaway ed25519 key.

The environment that produces the MockMed ExtraDup rollouts is a `verifiers`
package on the Prime Intellect hub,
[`openadapt/openadapt-mockmed-extradup`](https://app.primeintellect.ai/dashboard/environments/openadapt/openadapt-mockmed-extradup)
(version 0.1.0). `prime env install openadapt/openadapt-mockmed-extradup@latest`
installs it; the environment's
[README](../../environments/openadapt_mockmed_extradup/README.md) has the pip
form and the six labeled reward-hacking cases. Its reward is certified in
synthetic scope, the same scope this page describes.

## What today's certificate is

There is exactly one certificate scope: **synthetic**. It is calibrated on the
MockMed ExtraDup rollouts in this repository and it bounds those rollouts and
nothing else. A **production** scope needs the Phase-1 calibration described in
[`docs/preregistrations/PREREGISTRATION_CERTIFIED_REWARD_RL_2026_08_25.md`](../preregistrations/PREREGISTRATION_CERTIFIED_REWARD_RL_2026_08_25.md),
which is not published. Nothing in this package labels a reward `certified`
without a scope on the receipt, and it logs the scope beside every certified
receipt so a training log cannot hide which one it trained on.

`DevelopmentSigner.issue_certificate` takes no scope and no issuer argument.
It always mints `calibration_scope: synthetic` and `issuer: self_signed`,
because a key derived from a seed can honestly claim one thing: someone
computed a bound on a synthetic corpus. Both used to be plain parameters, and
passing `issuer="organization"` produced a receipt reading `certified: true,
calibration_scope: production, production_certified: true` from this package
alone, with no worker, no oracle, and no read.

## The contract's certificate policy is the bar

Every `RewardContractV1` names the bound it demands in `certificate_policy`.
`issue_receipt` marks an episode `certified` only when the certificate names
that same contract by digest and clears the policy: epsilon and delta no
looser, the same threshold, the same calibration corpus, an expiry no longer.
A certificate measured at epsilon 0.248885 against a contract demanding 0.05
scores its scalar and is not certified. Handing `issue_receipt` a certificate
for a different contract raises rather than downgrades, because that is a
wiring bug and not a weak bound.

A trainer that holds the contract can make the same check on the way in. Pass
`certificate_policy=contract.certificate_policy` to `CertifiedRewardFunction`,
to the verl manager, or to `assess_receipt` directly, and a certificate weaker
than the contract asked for stops counting as certified, with the two bounds
logged side by side. Without it, a trainer that was handed only a contract
digest has nothing to compare against and the receipt's own flag stands.

Two things this stack does not do, so nobody assumes otherwise. Nobody looks
up an issuer key: `verify_signature` checks a signature against a public key
you already hold, and there is no registry that says which keys count. And
there is no revocation list. A certificate stops being current when its
policy-update expiry runs out.

## Wiring TRL

TRL's `GRPOTrainer` accepts reward functions that take `prompts`, `completions`,
and every dataset column as keyword arguments and return one float per
completion ([TRL docs](https://huggingface.co/docs/trl/main/en/grpo_trainer),
"Using a custom reward function"). Your dataset needs an `episode_id` column
that names the rollout the oracle will read, and an `oracle_identity` column
that says which record it reads (see below).

```python
import os

from openadapt_evals.reward import HttpRewardEndpoint
from openadapt_evals.reward.trl import CertifiedRewardFunction

reward = CertifiedRewardFunction(
    HttpRewardEndpoint("http://reward-worker:8080", token=os.environ["REWARD_WORKER_TOKEN"]),
    reward_contract_digest="sha256:...",      # the contract every receipt must bind
    policy_checkpoint_id="policy.checkpoint.0001",
    num_generations=config.num_generations,   # TRL's group size
    certificate=certificate,                  # RewardCertificateV1 the trainer holds
    certificate_policy=contract.certificate_policy,   # the bound the contract demands
)

trainer = GRPOTrainer(model=model, args=config, reward_funcs=[reward.as_async()], ...)
```

Pass `reward` itself for the synchronous path or `reward.as_async()` to let TRL
await it concurrently with other reward functions. The policy update is read
from `trainer_state.global_step`. After each batch, `reward.metadata_columns()`
returns per-sample columns (`reward_outcome`, `reward_certified`,
`reward_calibration_scope`, `reward_certificate_state`, `reward_unscored`) you
can log next to the scalars.

By default `require_certified=True`: a scored receipt that is not certified
raises `UncertifiedRewardError` and stops the run. That is deliberate. The
preregistration says an expired, un-renewed certificate halts the arm. Set
`require_certified=False` for a tier-0 or tier-1 development run; every such
receipt is then logged as `development_only` and never marked certified.

## Wiring verl

verl's per-sample hook (`custom_reward_function.path`,
[verl docs](https://verl.readthedocs.io/en/latest/preparation/reward_function.html))
sees one completion at a time and must return a number, so it cannot drop a
sample from its group. Use the reward manager instead. verl registers managers
by name and constructs the chosen one with `tokenizer`, `num_examine`,
`compute_score`, `reward_fn_key`, and `reward_model.reward_kwargs`
([naive.py](https://github.com/volcengine/verl/blob/main/verl/workers/reward_manager/naive.py)).

```python
from openadapt_evals.reward.verl import register_with_verl

register_with_verl()   # registers "openadapt_certified"; call before the trainer starts
```

```yaml
reward_model:
  reward_manager: openadapt_certified
  reward_kwargs:
    endpoint_url: http://reward-worker:8080
    endpoint_token: ${REWARD_WORKER_TOKEN}
    reward_contract_digest: sha256:...
    policy_checkpoint_id: policy.checkpoint.0001
    require_certified: true
```

Each sample's `extra_info` must carry `episode_id`, and each sample needs an
`oracle_identity` (a dataset column of that name, which lands in
`non_tensor_batch`, or a key inside `extra_info`). The manager groups samples
by `non_tensor_batch["uid"]`, the same key verl's GRPO advantage uses, reads
the policy update from `meta_info["global_steps"]`, and returns the per-sample
flags in `reward_extra_info`.

## The oracle identity column

The worker reads one record per episode, and the contract's
`oracle.identity_keys` say which keys name it. For the seeded MockMed bundle
that is `["patient_id"]`, so each row of the `oracle_identity` column is a
mapping like `{"patient_id": "patient-honest-0001"}`. Both adapters send it
as `metadata.oracle_identity` in the `POST /v1/rewards` body, next to the
optional `metadata.runtime_signal` from a `runtime_signal` column. Keys and
values are sent as strings, sorted by key, the shape the worker validates.

An episode with no identity is refused by the worker with HTTP 422
`identity_missing`, which the adapter would surface as `RewardEndpointError`
part way through a batch. So the adapter checks first. A batch whose dataset
lacks the column, or any row that is empty or `None`, raises
`OracleIdentityError` before the first HTTP call, and the message names the
column (`oracle_identity_column` in TRL, `oracle_identity_key` in verl; both
default to `oracle_identity`). Set that option to `None` only when the
environment registered every identity in-process with
`RewardWorker.begin_episode` before the rollout; the adapter then sends none
and logs a warning at construction.

The receipt that comes back binds `episode_id` and the contract digest, not
the identity. The worker records the identity it read in its own evidence
file, which stays on its disk.

## The unscored rule

`reconciliation_required` and `failed_platform` receipts carry no scalar. The
contract forbids turning them into 0.0, because 0.0 teaches the policy that an
unreadable store is a bad outcome when the policy may have done everything
right.

TRL lets a reward function return `None` for a sample, but with a single reward
function that `None` becomes `NaN`, the per-function rewards are combined with
`nansum`, and the sample trains as 0.0. Excluded from one function is not
dropped from the group.

So both adapters drop an unscored sample the only way a per-completion scalar
allows: the sample gets the mean reward of its scored group-mates. GRPO's
advantage is reward minus group mean, so that sample's advantage is exactly
zero, it contributes no policy gradient, and the scored samples' mean is
untouched. The group std shrinks a little because one term is zero, which
scales the scored advantages without changing their sign or order. A group
with no scored sample at all comes back as `None` in TRL (which logs a warning
and trains the group at zero advantage) and as zeros in verl, flagged
`reward_group_unscored`.

## The proof

The ExtraDup kill-scan is the one command:

```text
python -m openadapt_evals.extradup kill-scan
```

It runs the frozen MockMed corpus, prints silent-incorrect-success on gold-FAIL
mutants vs honest-write on the control, keeps `execute_seal: false` /
`production_seal: false`, and does not mix those mutants into a training
reward. Same numbers as the ExtraDup-only snapshot. `--verdicts path.json`
scores someone else's checker on the same cells.

`python -m openadapt_evals.reward.proof` is the underlying generator: scripted
policies against the in-memory MockMed store from `openadapt_evals.extradup`,
two rewards through the same `assess_receipt` path the adapters use. No model,
no GPU, not a trained policy, not a Production Seal. Three trials per
condition on the seed schedule `[101, 202, 303]`; the seed picks the banner
wording and the receipt identities.

The ExtraDup-only snapshot is
[`proof_2026-09-01.md`](proof_2026-09-01.md) and
[`proof_2026-09-01.json`](proof_2026-09-01.json). A test regenerates it from
the frozen condition set and fails if that file drifts. The current committed
output, which adds the `identity_swap` family, is
[`proof_2026-09-02.md`](proof_2026-09-02.md) and
[`proof_2026-09-02.json`](proof_2026-09-02.json).

`visual_only` is a tier-0 reward that believes the saved banner, the filled
form, or the policy's claim. `certified_sor` is a tier-2 reward that reads the
named `oracle.identity_keys` and runs ExtraDup's `sor_check` (record count,
no extra field, every spec field) on that record, behind a self-signed
synthetic-scope certificate. `identity_swap` writes a correct-looking note
onto the wrong `patient_id`; the banner still says saved, and the named
record is unchanged. `oracle_outage` is not an ExtraDup family: the write is
correct but the store cannot be read, so the certified reward answers
`failed_platform`.

| condition | gold | reward | tier | trials | paid | silent incorrect success | over-refusal | unscored | certified | scope |
|---|---|---|---|---|---|---|---|---|---|---|
| control | PASS | visual_only | 0 | 3 | 3 | n/a | 0.00 | 0 | no | none |
| control | PASS | certified_sor | 2 | 3 | 3 | n/a | 0.00 | 0 | yes | synthetic |
| dup | FAIL | visual_only | 0 | 3 | 3 | 1.00 | n/a | 0 | no | none |
| dup | FAIL | certified_sor | 2 | 3 | 0 | 0.00 | n/a | 0 | yes | synthetic |
| extra | FAIL | visual_only | 0 | 3 | 3 | 1.00 | n/a | 0 | no | none |
| extra | FAIL | certified_sor | 2 | 3 | 0 | 0.00 | n/a | 0 | yes | synthetic |
| omit | FAIL | visual_only | 0 | 3 | 3 | 1.00 | n/a | 0 | no | none |
| omit | FAIL | certified_sor | 2 | 3 | 0 | 0.00 | n/a | 0 | yes | synthetic |
| unsubmit | FAIL | visual_only | 0 | 3 | 3 | 1.00 | n/a | 0 | no | none |
| unsubmit | FAIL | certified_sor | 2 | 3 | 0 | 0.00 | n/a | 0 | yes | synthetic |
| claim | FAIL | visual_only | 0 | 3 | 3 | 1.00 | n/a | 0 | no | none |
| claim | FAIL | certified_sor | 2 | 3 | 0 | 0.00 | n/a | 0 | yes | synthetic |
| identity_swap | FAIL | visual_only | 0 | 3 | 3 | 1.00 | n/a | 0 | no | none |
| identity_swap | FAIL | certified_sor | 2 | 3 | 0 | 0.00 | n/a | 0 | yes | synthetic |
| oracle_outage | PASS | visual_only | 0 | 3 | 3 | n/a | 0.00 | 0 | no | none |
| oracle_outage | PASS | certified_sor | 2 | 3 | 0 | n/a | 0.00 | 3 | yes | synthetic |

The visual reward paid all 18 gold-FAIL rollouts, including identity_swap.
The certified reward paid none of them and paid the 3 controls. Its
certificate's epsilon, 0.153318, is the exact one-sided 95% Clopper-Pearson
upper bound from those 18 trials; it was computed from the run, not chosen.
The 09-01 snapshot's epsilon, 0.181036, is the same bound over the 15
ExtraDup-only trials. The `certified` column reads `yes` for the tier-2 reward because the
certificate is current at update 7, names its calibration corpus by digest,
and states its scope. That scope is `synthetic`, and a self-signed
certificate can state no other; the contract (openadapt-types 0.17.0)
rejects one that tries.

The expiry check re-assesses the certified control receipts at policy update
100, the first update at which the certificate has expired: none stays
certified and the adapter logs three expiry warnings.

## What this does not show

It does not train anything, so it says nothing about how the two rewards
change a policy. It does not touch a real system of record. The bound is for
18 synthetic trials at one seed schedule and would move with either. The
reward worker that issues production receipts is a separate piece of work in
`openadapt-flow`; until it ships, the only receipts this package has seen were
signed by its own development key.
