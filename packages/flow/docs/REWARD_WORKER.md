# The reward worker

`openadapt-flow serve-reward` scores one training episode by reading the
system of record after the episode ends. It answers one question: did the
terminal effect the reward contract requires actually land, and did nothing
the contract forbids land with it? The answer comes back as a signed
`RewardEvidenceReceiptV1` from `openadapt-types`.

A reward receipt is not an Execute Seal. Execute takes a qualified program
with zero model use. A model rollout is not one, so it never receives an
Execute receipt, and the reward receipt never claims that Flow governed the
policy's actions. The two receipts carry different schema ids, and the
reward receipt has no `execution_id`, `workflow_digest`, `qualification_id`,
or `contracts` block, so one cannot be passed off as the other.

## What runs where

Three processes, on three machines, and only one of them sees the data.

The **organization worker** is this command. It runs inside the customer
network, next to the system of record, and holds the only credential that
can read it. It reads through one oracle recipe (a REST document, a
read-only SQL query, a FHIR search, a directory listing, or a JSON dump for
the synthetic fixture), judges the read, and signs the receipt with a local
Ed25519 key under `~/.openadapt/reward-ref/`. Evidence bytes (the records it
read, the verdicts) stay on that disk. The receipt carries only digests.

The **OpenAdapt control service** is off the high-volume path. It issues
reward certificates and publishes the calibration corpus digest a
certificate names. It never sees an episode. Today the only certificate that
exists is synthetic scope, signed by the worker's own key for the MockMed
fixture. A production-scope certificate needs the Phase-1 calibration on a
held-out corpus, which is not published. Nothing revokes a certificate yet;
expiry counts policy updates and is the only thing that ends one.

The **trainer node** runs the policy and the optimizer. It submits an episode
descriptor to the worker and gets the receipt back. The descriptor is the
shape `openadapt_evals.reward.receipts.EpisodeDescriptor` sends:
`episode_id`, `policy_checkpoint_id`, `policy_update`,
`reward_contract_digest`, and optional `task_id`, `environment_id`, and
`metadata`. The digest must be the contract this worker serves, or the
episode is refused. The trainer never gets a credential for the system of
record. The trainer-side adapters live in `openadapt_evals.reward`
(`pip install 'openadapt-evals>=0.96.0'`), not in this package.
`openadapt_flow.reward.callables` keeps only `HttpRewardClient`, the payload
builder, and the receipt's scalar, for the trip between the two machines.

## Register the episode before the rollout runs

The environment calls `POST /v1/episodes`, or `RewardWorker.begin_episode`
in process, with the episode id and the subject. Two facts get fixed there,
and both decide what the receipt may say later.

The **subject** is the record the oracle reads. Registration happens before
anyone knows how the episode will turn out, and the descriptor arrives after,
so the registration wins. A descriptor may repeat the registered subject and
gets a 422 `identity_conflict` if it names a different one. Re-registering
the same episode under a different subject is refused too. Without a
registration the worker still accepts `oracle_identity` on the descriptor, or
`metadata.oracle_identity`, and the keys must match the contract's
`identity_keys` exactly.

The **baseline** is the store as it stood before the rollout. Required
effects have to include at least one claim about change (`count_new_only` on
a `record_written` effect, or an `exact_new_set` effect), so without a
baseline the judge rules INDETERMINATE and the episode is unscored. That is
what stops a rollout that did nothing from collecting the full reward on a
row that was already there. `RewardBundle.load` refuses a contract whose
required effects only describe the store's current contents.

Matching keys does not mean the oracle read the right record: it returns the
whole collection, so a required effect must select the subject with a
`{param: ...}` reference, and `RewardBundle.load` refuses a bundle where none
does (`docs/EFFECT_KIT.md`).

## The policy update only moves forward

A certificate expires after a stated number of policy updates, and
`policy_update` on the descriptor is the only thing that moves an episode
towards that expiry. It arrives on the wire from the trainer, so the worker
keeps its own ledger under `<data-dir>/policy_updates/` holding the highest
update this contract has scored, and refuses anything below it with 422
`policy_update_regressed`. Count 0, then 999, then 10^9, and the certificate
reads expired from then on; counting back to 0 is refused rather than
honoured.

The mark is per contract, not per policy checkpoint. Per checkpoint, a
trainer could rename its checkpoint and start counting from zero again, and
expiry would never arrive.

## The outcome table

The worker judges every required effect and every forbidden effect with
the same three-valued judge the runtime uses
(`openadapt_flow/runtime/effects/_common.py`). The runtime's own signal
about how the episode ended is an input, never the verdict. The rules fire in
this order.

| Condition | `reward_outcome` | Scalar |
|---|---|---|
| Runtime signal `failed_platform` | `failed_platform` | unscored |
| Store unreachable at read time | `failed_platform`, uncertainty `oracle_unavailable` | unscored |
| Any verdict INDETERMINATE (stale read, no baseline for `count_new_only`) | `reconciliation_required`, uncertainty `effect_uncertain` | unscored |
| Any forbidden effect present | `wrong_effect` | 0 or the declared penalty |
| Signal `completed`, every required effect CONFIRMED | `verified` | the declared positive reward |
| Signal `completed`, a required effect REFUTED (absent, duplicated, wrong value) | `wrong_effect` | 0 or the declared penalty |
| Signal `halted_before_effect`, `refused`, or `rejected_policy`, store shows no required effect | that outcome | 0 or the declared penalty |
| Same signals, but a required effect is present anyway | `reconciliation_required`, uncertainty `effect_uncertain` | unscored |

Unscored is never 0.0. The receipt carries `scalar_reward: null` and the
envelope says `unscored: true`. Zero would teach the policy that an
unreadable store is the same as a wrong write. It is not.

`certified` is true only at oracle tier 2 or 3 with a certificate that is
current at the episode's policy update. Tier 0 (visual, OCR) and tier 1
(second UI session) receipts are `development_only` and can never be
certified, whatever the screen shows. A verified tier-2 receipt whose
certificate expired is still `verified`, still scored, and not certified.

## What the tier rests on

The recipe kind picks the channel and the channel sets the tier, so the tier
has to rest on a mechanism the worker can check rather than on a word the
bundle author chose.

`json_file` and `screen_dump` both hand `JsonDocumentOracle` a JSON document
on the worker's own disk. Nothing in those bytes separates a
system-of-record dump from a screen scrape, so both read through `ocr` at
tier 0, and a contract that declares `file` for a `json_file` recipe fails to
load. One adapter, one channel: the same document cannot be worth tier 0
under one kind and tier 2 under the other.

`sqlite` is tier 2 because the worker opens a real database read-only and
runs one SELECT through the engine. It checks the file header, so a screen
dump renamed `store.db` is refused before the worker starts. `build_oracle`
also refuses to hand back any adapter whose channel differs from the recipe
table's, so the two places that have to agree about a tier cannot drift
apart quietly.

`rest` and `fhir` are tier 2 on the strength of the read being a network
call to an endpoint the bundle names. The worker verifies that it made that
call. It cannot verify that the endpoint is the customer's system of record
rather than a server the trainer stood up, and a self-signed synthetic-scope
certificate does not attest to it. Whoever admits the bundle owns that
check.

## The MockMed run

The reward worker is not in a published release yet, and the release
carrying it has no date. These two commands are what will work once it
lands:

```bash
pip install 'openadapt-flow[reward]'
openadapt-flow serve-reward --seed-mockmed --port 8788
```

Until then, run it from a checkout of the repository head:

```bash
git clone https://github.com/OpenAdaptAI/openadapt-flow
cd openadapt-flow && pip install -e '.[reward]'
python -m openadapt_flow serve-reward --seed-mockmed --port 8788
```

`--seed-mockmed` writes two contract bundles and their stores under the data
directory and serves the tier-2 one when `--contract` is omitted.

`contracts/mockmed` reads `mockmed/records.db` through the `sqlite` recipe,
channel `db`, tier 2. The database starts with two rows for the duplicate
patient and nothing for anybody else, because the required effect asserts a
change: an episode has to write something before it can be verified.

Before it signs the synthetic certificate the seed runs 300 trials through
the bundle's own judge. The records those trials plant come from the
bundle's own required and forbidden effects, so the fault lands on the rows
this contract talks about. Each trial plants one of six faults: an extra
record, a duplicate, a missing record, a wrong field value, the right record
under another patient, or a forbidden discharge. The certificate's `epsilon`
is the exact one-sided 95% Clopper-Pearson bound from those counts, the same
method the evals proof run uses (its 0 of 15 gives 0.181036). With 300
trials and zero false accepts the bound is 0.0099. `calibration.json` beside
the certificate records the trial count, the false-accept count, which fault
classes applied, and the corpus digest, so you can recompute the bound and
see what it covers. The certificate carries `calibration_scope: synthetic`
and `issuer: self_signed`; the types contract refuses a self-signed
certificate with any other scope.

The corpus digest is derived from the contract, and `RewardBundle.load`
refuses a certificate that names a different one. A bound measured on
somebody else's records does not apply to these effects.

`contracts/mockmed-tier0` reads `mockmed/screen.json` through the
`screen_dump` recipe, channel `ocr`, tier 0.

Three episodes to run, with the bearer token and contract digest the banner
prints. Each one registers first, then writes what the episode would have
written, then asks for the reward:

```bash
TOKEN=...    # printed on start, also in ~/.openadapt/reward-ref/token
DIGEST=...   # printed on start as "digest", also GET /health
DB=~/.openadapt/reward-ref/mockmed/records.db
call() { curl -s -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d "$2" "http://127.0.0.1:8788$1"; }
save() { sqlite3 "$DB" \
  "INSERT INTO encounters (patient_id, type, status) VALUES ('$1', 'Triage', 'saved')"; }

# 1. The honest episode: registered, wrote its row, verified.
call /v1/episodes '{"episode_id":"episode_honest_01",
       "oracle_identity":{"patient_id":"patient-honest-0001"}}'
save patient-honest-0001
call /v1/rewards '{"episode_id":"episode_honest_01","policy_checkpoint_id":"policy_checkpoint_mockmed_0",
       "policy_update":0,"reward_contract_digest":"'$DIGEST'",
       "metadata":{"oracle_identity":{"patient_id":"patient-honest-0001"}}}'
# -> reward_outcome verified, scalar_reward 1.0, certified true,
#    calibration_scope synthetic

# 2. The banner lie: registered, wrote nothing to the database.
call /v1/episodes '{"episode_id":"episode_lie_01",
       "oracle_identity":{"patient_id":"patient-lie-0002"}}'
call /v1/rewards '{"episode_id":"episode_lie_01","policy_checkpoint_id":"policy_checkpoint_mockmed_0",
       "policy_update":0,"reward_contract_digest":"'$DIGEST'",
       "metadata":{"oracle_identity":{"patient_id":"patient-lie-0002"}}}'
# -> reward_outcome wrong_effect, scalar_reward 0.0. The screen said saved.
#    The store holds no record.

# 3. The double submit: two rows where the contract allows one.
call /v1/episodes '{"episode_id":"episode_dup_01",
       "oracle_identity":{"patient_id":"patient-dup-0003"}}'
save patient-dup-0003 && save patient-dup-0003
call /v1/rewards '{"episode_id":"episode_dup_01","policy_checkpoint_id":"policy_checkpoint_mockmed_0",
       "policy_update":0,"reward_contract_digest":"'$DIGEST'",
       "metadata":{"oracle_identity":{"patient_id":"patient-dup-0003"}}}'
# -> reward_outcome wrong_effect. Two new Triage records.
```

Register an episode, write nothing, and score it, and the answer is
`wrong_effect` with `scalar_reward` 0.0 even when the patient already has a
row. The required effect asks what this episode added.

Then the tier-0 worker, in a second terminal, on the same banner-lie
episode. Write the banner into the screen dump and nothing into the
database, and the two channels disagree:

```bash
openadapt-flow serve-reward --contract ~/.openadapt/reward-ref/contracts/mockmed-tier0 --port 8789
DIGEST0=...  # that bundle's own digest, from its banner or GET /health
call0() { curl -s -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d "$2" "http://127.0.0.1:8789$1"; }

call0 /v1/episodes '{"episode_id":"episode_lie_02",
       "oracle_identity":{"patient_id":"patient-lie-0002"}}'

# The banner appears. Nothing reaches the database.
python - <<'PY'
import json, pathlib
p = pathlib.Path.home() / ".openadapt/reward-ref/mockmed/screen.json"
body = json.loads(p.read_text())
body["records"].append({"id": 900, "patient_id": "patient-lie-0002",
                        "type": "Triage", "status": "saved"})
p.write_text(json.dumps(body, indent=2))
PY

call0 /v1/rewards '{"episode_id":"episode_lie_02","policy_checkpoint_id":"policy_checkpoint_mockmed_0",
       "policy_update":0,"reward_contract_digest":"'$DIGEST0'",
       "metadata":{"oracle_identity":{"patient_id":"patient-lie-0002"}}}'
# -> reward_outcome verified, development_only true, certified false.
#    The OCR dump agrees with the banner while the database holds nothing.
#    That is why tier 0 cannot certify.
```

The MockMed banner lie fixture yields 0 because the seeded contract declares
`wrong_effect_reward: 0.0`. The contract default is -1.0. A penalty is a
training choice the contract states; the worker never picks one.

## Routes

| Route | Body in | Body out |
|---|---|---|
| `GET /health` | none | issuer, key fingerprint, contract digest, oracle tier |
| `POST /v1/episodes` | `episode_id` and `oracle_identity` | the registered subject and whether the baseline read |
| `POST /v1/rewards` | the episode descriptor | the self-signed envelope, 200, receipt under `receipt` |
| `GET /v1/rewards/{receipt_id}` | none | the stored envelope |
| `POST /v1/graders/openai` | `{"sample": ..., "item": ...}` | `{"score": 0..1, ...}` or 422 |

Every route but `/health` needs `Authorization: Bearer <token>`. The
envelope carries `issuer: self_signed`, `execute_seal: false`,
`production_seal: false`, `flow_governed_policy: false`, `unscored`, and the
receipt. It has no top-level `schema_version`, which is how the evals client
tells an envelope from a bare receipt. Submitting the same `episode_id`
twice returns 409; a reward is issued once.

The 422 cases, all of them refusals to sign something the read does not
support:

| `error` | When |
|---|---|
| `contract_mismatch` | the descriptor names a contract this worker does not serve |
| `identity_missing` | no registration, no `oracle_identity`, no `metadata.oracle_identity` |
| `identity_mismatch` | the subject's keys are not the contract's `identity_keys` |
| `identity_conflict` | the descriptor names a subject other than the registered one |
| `policy_update_regressed` | the policy update is below the highest this contract has scored |
| `invalid_episode` | the descriptor carries a rollout, screenshot, or PHI field |

The OpenAI route mirrors the only custom-grader contract OpenAI documents,
the `python` grader's `grade(sample, item) -> float` (graders guide and
reinforcement fine-tuning guide at developers.openai.com, read 2026-09-01).
OpenAI documents no grader that calls a user-hosted URL, and its python
grader runs without network access, so a hosted RFT job cannot reach this
worker. The route exists for a self-hosted loop that already speaks that
shape. Its schema has no "do not score" value, and OpenAI's own rule is that
an exception or a bad float "will be marked as invalid and return a 0
grade". This worker refuses that: an unscored episode answers 422 with
`error: unscored`, and a wrapper must drop the sample before any grader
sees it.

## Trainer adapters

The adapters for TRL's `GRPOTrainer` and verl's reward manager live in
`openadapt_evals.reward`: `CertifiedRewardFunction` (TRL) and
`CertifiedRewardManager` (verl). Install them on the trainer node:

```bash
pip install 'openadapt-evals>=0.96.0'
```

Both call `openadapt_types.score`, read the receipt's own fields, and refuse
the combinations a trainer must never accept: an unscored episode is removed
from its GRPO group, a `development_only` receipt is never labelled certified
(and the only certificate scope in use today is synthetic), and in
`require_certified` mode an expired certificate stops the run. The wiring,
with a code sample for each trainer, is in the evals package's
`docs/reward/README.md` and on
[docs.openadapt.ai](https://docs.openadapt.ai/commercial/seal-reward/).

This package offers no trainer-facing reward function, and that is on
purpose. TRL lets a reward function return `None` for a sample, but
`GRPOTrainer` turns that `None` into NaN, combines the per-function rewards
with `nansum`, and takes the group mean over the result. With one reward
function the `None` row trains as 0.0, which is what the contract forbids for
`reconciliation_required` and `failed_platform`. verl's per-sample
`compute_score` hook must return a number, so it cannot drop a sample either.
The evals adapters drop an unscored episode the one way a per-completion
scalar allows: the episode gets the mean reward of its scored group-mates, so
its advantage is exactly zero and the scored mean is unchanged.

The dependency runs one way. openadapt-evals depends on openadapt-flow, so
flow cannot import the adapters, and a second copy here would drift from the
first.

## Scope

The worker reads the store once after the episode and signs what it read.
It never sees the screen or the trajectory, so it cannot grade how the
policy got there. If the store cannot be read, it says so and scores
nothing.
