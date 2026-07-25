# Execution profiles

`openadapt-flow run` applies one named posture over the existing policy,
identity, effect, authorization, durability, and evidence machinery:

| Profile | Contract | Successful report |
| --- | --- | --- |
| `demo` | Permits uncertified tutorials and screen evidence. Integrity checks and runtime refusals still apply. | `COMPLETED_UNVERIFIED`; never production-eligible |
| `standard` | Requires certification, a sealed manifest, durable and settled-state execution, identity coverage for consequential actions, and effect evidence at the configured minimum tier for every consequential effect. Application-level encryption is optional when the qualified deployment supplies an encrypted storage boundary; an encrypted bundle always produces encrypted checkpoints. | `VERIFIED` only when the complete runtime contract passes |
| `regulated` | Standard plus encrypted bundle contents, strictly sealed evidence assets, and encrypted durable checkpoints in the customer-controlled environment. Model egress remains off unless explicitly authorized and PHI allowlisted. | `VERIFIED` only when the complete runtime contract passes |

Select the profile in deployment configuration:

```yaml
runtime:
  profile: regulated
```

or for one invocation:

```bash
openadapt-flow run bundle --config deployment.yaml --profile standard
```

Raw `replay` is the Demo path. For compatibility, an existing `run` invocation
that selects no profile retains the pre-profile low-level flag behavior and
legacy report fields. New production deployments should select `standard` or
`regulated` explicitly.

Named profiles do not replace policy certification. A policy describes what the
bundle must contain; the profile determines which admission and runtime
properties are mandatory for this execution.

Low-level flags can strengthen a profile. They cannot weaken a selected
Standard or Regulated contract. In particular:

- Standard and Regulated require effect evidence at the configured minimum
  tier; an operator approval cannot turn an immediate-screen-only or
  unverified write into `VERIFIED`.
- Regulated refuses `--allow-unencrypted` and requires
  `OPENADAPT_BUNDLE_KEY`; the same key seals its durable checkpoints. Standard
  accepts a qualified external encrypted-storage boundary, but if its bundle is
  application-sealed the runtime requires and reuses that key for checkpoints.
- Standard and Regulated enable durable execution automatically.
- Standard and Regulated require settled-state detection.
- A successful Demo remains `COMPLETED_UNVERIFIED`, even when every tutorial
  step completed.

Reports retain the legacy `success` field for compatibility and add
`execution_profile`, `execution_outcome`, and `production_eligible`. Production
callers must use `execution_outcome`; Standard and Regulated treat
`COMPLETED_UNVERIFIED` as a non-success exit.
