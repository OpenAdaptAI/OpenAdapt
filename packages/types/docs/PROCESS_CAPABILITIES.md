# Process artifacts and code capabilities

An OpenAdapt process can now name the files and programs that sit between GUI
workflows. The public contracts do not execute them. They give Flow and another
runner the same exact objects to validate.

## The four contracts

`ArtifactRefV1` is a path-free handle for one immutable file. It records a
SHA-256 digest, byte size, media type, producer, storage boundary, data class,
and verifier state. The file stays in the customer-controlled store.

`CodeCapabilityManifestV1` identifies one Python program. The manifest binds
the source archive, dependency lock, direct entry point, typed I/O schemas,
declared outputs, permissions, effect contract, oracle contract, and
qualification campaign. A manifest digest proves which program was selected.
It doesn't prove that the program is correct.

`CodeCapabilityAdmissionEnvelopeV1` gives an exact manifest a signed authority
window of at most 30 days. The runtime still has to reproduce the live
environment and every bound contract before it starts the program.

`ProcessEvidenceReceiptV1` is the terminal root receipt. It references the
child receipts, human receipts, artifact graph, and evidence root by digest.
Evidence bytes stay inside their declared boundary.

## Example

```python
from openadapt_types import CodeCapabilityManifestV1

manifest = CodeCapabilityManifestV1.model_validate_json(
    open("code-capability.json", encoding="utf-8").read()
)

print(manifest.digest)
```

The runtime executes `entrypoint` as an argument array. It doesn't pass the
entry point through a shell. Input artifacts arrive through named mounts, and
the program can write only to its declared output mount when the runner can
enforce that rule.

The first runner profile is `trusted_local`. It records exact code and policy,
but it doesn't claim that a subprocess supplies a security boundary. A
production deployment needs an enforced OS sandbox, container, or virtual
machine profile.

## Verification

A code capability cannot mark its own output as verified. A separate oracle
checks the declared effect. For a generated report, that check can include the
account, period, row count, and totals from the immutable source artifact.

The final process receipt uses the Execute outcome vocabulary. It can report
`verified`, `halted_before_effect`, `reconciliation_required`,
`rejected_policy`, `failed_platform`, or `rolled_back_verified`.

A `verified` process receipt requires:

- Oracle tier 2 or 3.
- No uncertain delivery.
- No model call during the run.
- At least one child receipt.

The model that wrote the program is not part of the runtime contract. A repair
creates a new source digest, manifest digest, and admission.
