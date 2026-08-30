# Parameters, profiles, and run outcomes

## You don't have to name parameters up front

The recorder passively captures each typed field's label (DOM/accessibility, or
nearby OCR on pixel paths), and `compile` proposes a parameter named from it
(`"Insurance No."` -> `insurance_no`). Proposals are never applied silently:
`compile` lists them once for confirm / rename / mark-secret / keep-constant (on
a TTY), or non-interactively via `--accept-params insurance_no` /
`--params-from decisions.json`; unconfirmed values stay exactly as demonstrated.
An explicit `--param` always wins and suppresses the proposal.

Recorded parameter values are the defaults, and `--param` overrides them at
replay. Drive a real deployment with a config bound to the bundle digest:

```bash
python -m openadapt_flow.cli_config init bundle --out deploy.yaml
# Review and complete every path in `unresolved`. Do not put secrets in the file.
openadapt-flow certify bundle --config deploy.yaml
openadapt-flow run bundle --profile standard --config deploy.yaml
```

`certify` and `run` refuse the draft until those fields are filled.
`openadapt-flow run bundle --config deploy.yaml` then reads the backend,
effects, identity, idempotency, actuation, durable, and policy sections from
one config.

## Choose an execution profile

Select `--profile regulated` for encrypted, fail-closed production execution,
`--profile standard` for a certified and durable deployment whose qualified
storage boundary may supply at-rest encryption, or `--profile demo` for an
explicitly non-production run.

Demo completions are `COMPLETED_UNVERIFIED`; Standard and Regulated return
`VERIFIED` only when every consequential effect is confirmed at the workflow's
configured minimum evidence tier. The full contract per profile is in
[`EXECUTION_PROFILES.md`](EXECUTION_PROFILES.md).

## Every run states its transaction outcome

Every run carries a first-class `transaction_outcome` that states what is known
about the business effect, plus a per-step effect journal:

| `transaction_outcome` | Meaning |
| --- | --- |
| `VERIFIED` | Every consequential effect was confirmed at the configured minimum evidence tier |
| `HALTED_BEFORE_EFFECT` | The run stopped before the consequential action executed |
| `RECONCILIATION_REQUIRED` | Delivery is uncertain; an operator reconciles it, and the run never blind-retries the write |
| `FAILED_PLATFORM` | The platform or substrate failed |
| `CANCELED` | The run was canceled |
| `REJECTED_POLICY` | A policy refused the run |
| `COMPLETED_UNVERIFIED` | A Demo-profile completion; never billable and never a success |

## Compiled is not the same as certified safe

`lint` reports a bundle's coverage gaps (clicks that act with no identity check,
steps that assert nothing, write steps left mis-classified) with a severity
each; `certify` enforces a policy and exits nonzero, refusing the bundle before
it deploys, when it fails. Risk is auto-classified at compile time (write-shaped
clicks such as save / submit / create / delete become `irreversible`, which arms
the low-confidence refusal), and two example policies ship: a permissive default
and a strict `clinical-write.yaml`. See [`LIMITS.md`](LIMITS.md) for what the
heuristic does and does not catch.
