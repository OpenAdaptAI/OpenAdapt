# Receipt formats (draft)

This draft describes the signed Write Receipt and Execute issuer Seal in
Flow 2.6.0 and Types 0.24.0, at revision `ff5257cdb`. It specifies existing
formats; the linked models and checkers define their exact field constraints.

| Artifact | Format covered here |
| --- | --- |
| Signed Write Receipt | `openadapt.write-receipt/v1`, inside COSE_Sign1 |
| Execute receipt with Cloud issuer Seal | `openadapt.execute-evidence-receipt/v1` with `openadapt.seal-signature/v1` |

[Effect receipts](VERIFY_RUN.md#the-receipt), [local Seals](OFFLINE_SEALS.md)
and [report-run receipts](RECEIPTS.md) have separate formats and checkers.

## Signed Write Receipt

[`ReceiptPayload`](../openadapt_flow/write_receipt.py) has these wire fields.
Unknown fields are refused, including in the nested models.

| Fields | Contents |
| --- | --- |
| `schema`, `source`, `verdict` | Schema above; `verifier`, `shadow.sql` or `shadow.fhir`; `CONFIRMED`, `REFUTED` or `UNCERTAIN` |
| `provisional`, `recheck_due_at`, `supersedes` | Recheck status, nullable due time and nullable predecessor receipt id |
| `checked` | Parts named `delta`, `frame`, `source`; values `held`, `refuted`, `not_checked`, `instruction` |
| `scope` | `read_paths`, `stores`, `unread`, `window`, `strength`, `not_covered` |
| `scope.window` | Nullable `baseline_read_at`, `read_at`, `max_execution_window_s` |
| `not_checked` | Scope exclusions, unread stores and the fixed limits below |
| `subject` | Opaque `write_id`; nullable `contract_sha256`, `verify_run_receipt_sha256` |
| `evidence_commitment`, `issued_at`, `issuer` | Salted evidence digest, timestamp and issuer key id |

The payload bytes are UTF-8 JSON from
[`canonical_json`](../../types/openadapt_types/seal.py): sorted keys, compact
separators and unescaped Unicode. This is the implementation's canonical form.
The verifier rejects repeated JSON keys and noncanonical payload bytes.

[`receipt_log`](../openadapt_flow/receipt_log.py) encodes a canonical CBOR,
tag-18 COSE_Sign1. Its protected header has `alg` (1) = -8 (EdDSA), content
type (3) = `application/vnd.openadapt.write-receipt+json`, issuer `kid` (4),
and CWT claims (15): issuer (1), subject/write id (2), issued-at epoch (6).
Ed25519 signs the COSE `Signature1` structure with empty external AAD.

The unprotected header carries one log receipt at label 394. That receipt
signs a detached tree root and carries the inclusion proof. The Write Receipt
id is lowercase hex `SHA-256(0x00 || bare_statement)`, where `bare_statement`
is the canonical COSE statement with an empty unprotected header. Changing
the log proof cannot rename the signed statement.

The evidence commitment is hex SHA-256 of
`b"openadapt.write-receipt-evidence/v1\0" + salt + canonical_evidence_utf8`.
The 32-byte salt and evidence stay in a separate private evidence file.

With `openadapt-flow[receipts]` installed, a holder checks offline against
independently trusted issuer and log public keys:

```bash
openadapt-flow receipt verify write.receipt.cbor --issuer-key issuer.pub.pem --log-key log.pub.pem --json
```

[`verify`](../openadapt_flow/write_receipt.py) checks the signature, exact
headers and claims, model, scope rules and log inclusion. `--evidence FILE`
also opens the commitment. `--no-log` explicitly omits inclusion checking;
`--require-anchor` additionally checks the supplied Rekor anchor offline.

A valid receipt can state `REFUTED` or `UNCERTAIN`. `CONFIRMED` is refused
without baseline/read-back times or a read store/path, at strength `unchecked`,
or with an unread store or refuted part. Outside `shadow.fhir`, `delta` must
be `held`. Missing scope exclusions or fixed limits, and inconsistent
recheck/window claims, are refused. A past-due provisional receipt produces
a warning; request its superseding receipt before relying on continued success.

## Execute receipt and issuer Seal

[`ExecuteEvidenceReceiptV1`](../../types/openadapt_types/execute.py) defines:

| Fields | Contents |
| --- | --- |
| `schema_version` | `openadapt.execute-evidence-receipt/v1` |
| `receipt_id`, `execution_id`, `workflow_version`, `qualification_id`, `environment_id`, `runner_id`, `nonce` | Opaque identifiers |
| `workflow_digest`, `evidence_digest`, `issued_at` | `sha256:`-prefixed digests and timestamp |
| `oracle_tier`, `outcome` | Evidence tier and terminal outcome |
| `contracts` | `authorization_passed`, `identity_passed`, `postcondition_passed`, `effect_passed`, `minimum_effect_strength`, nullable `observed_effect_strength`, `model_used`, `external_network_used` |
| `delivery_uncertain`, `compensation_effect_verified` | Delivery uncertainty and independently checked compensation |
| `seal_signature` | Optional issuer signature; absent or null means unsigned |

[`SealSignatureV1`](../../types/openadapt_types/seal.py) contains `schema`,
`issuer`, `algorithm`, `key_id`, `receipt_sha256`, `run_id`, `issued_at` and
`signature`. The fixed values are `openadapt.seal-signature/v1`,
`openadapt-cloud` and `ed25519`. The key id is `seal-ed25519-` plus the first
16 hex digits of SHA-256 of the raw 32-byte public key.

`receipt_sha256` hashes the canonical UTF-8 JSON of the original receipt
mapping with only its top-level `seal_signature` removed. Keep that original
mapping: adding model defaults changes the signed hash. Ed25519 signs
`b"openadapt.seal-signature/v1\0"` followed by canonical UTF-8 JSON of the
signature object without `signature`. The signature is base64.

For a bare Execute receipt and an independently pinned registry, check both
the model and signature, and bind the signature to the execution:

```python
from pathlib import Path
from openadapt_types.execute import ExecuteEvidenceReceiptV1
from openadapt_types.seal import parse_key_registry, verify_sealed_receipt
from openadapt_flow.offline_seal import loads_strict

receipt = loads_strict(Path("seal.json").read_bytes())
ExecuteEvidenceReceiptV1.model_validate(receipt)
keys = loads_strict(Path("trusted-keys.json").read_bytes())["keys"]
checked = verify_sealed_receipt(receipt, parse_key_registry(keys))
signature = receipt.get("seal_signature")
if not checked.ok or not signature or signature["run_id"] != receipt["execution_id"]:
    raise ValueError("issuer Seal refused")
print(receipt["outcome"])
```

Signature checking alone does not validate the receipt model or establish a
successful outcome. The model permits `verified` and `rolled_back_verified`
only with all four contracts passed, sufficient observed effect strength and
oracle tier at least 2; rollback also requires verified compensation.
`reconciliation_required` requires delivery uncertainty. The checker refuses
unsigned, malformed, mismatched, unknown-key and bad-signature inputs, or an
unavailable crypto verifier. A revoked key refuses signatures dated at or
after `revoked_at`.

## Trust and scope

Both formats authenticate the key holder's statement. Trust the issuer key
and registry through a separate channel; a key bundled with a receipt cannot
establish its own identity. A digest binds bytes, not their truth. A valid
signature does not prove that the verifier or its read path was correct, or
that the claimed issue time has an independent witness. A local log's
inclusion proof does not establish that its operator never rebuilt the log.

Read the outcome, scope, read window, evidence strength and recheck status.
Every Write Receipt excludes later writes, unlisted stores, authorization or
clinical correctness, and agent actions outside the checked write. Opening
an evidence commitment proves which evidence was bound; it does not rerun
the system-of-record read. Keep private evidence within its declared boundary.
