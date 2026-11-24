# Verify OpenAdapt Seal (GitHub Action)

Offline cryptographic verification for [OpenAdapt](https://github.com/OpenAdaptAI/OpenAdapt) run receipts (`record/run/v1` DSSE envelopes and `openadapt.receipt-card/v1` public cards).

- **Zero network egress** during verification: checks Ed25519 signatures and receipt invariants strictly against local pinned key material.
- **Fail-closed**: exits with code `1` on any missing receipt, missing/unknown key, malformed JSON, or tampered payload/signature.
- **GitHub Step Summary**: writes a concise verification table to `$GITHUB_STEP_SUMMARY` and exposes `verified`, `receipt-id`, `outcome`, and `reason` step outputs.

## Usage

```yaml
- uses: OpenAdaptAI/OpenAdapt/.github/actions/verify-seal@main
  with:
    receipt: path/to/receipt.json
    public-key: path/to/public-key.pem # Ed25519 PEM or openadapt.receipt-keys/v1 JSON
```

### Inputs

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `receipt` | Yes | — | Path to `receipt.json` (`record/run/v1` DSSE envelope or public card). |
| `public-key` | No* | `""` | Path to an Ed25519 public key `.pem` file or `openadapt.receipt-keys/v1` JSON registry. |
| `keys` | No* | `""` | Path to an `openadapt.receipt-keys/v1` pinned key registry JSON file. |
| `key-role` | No | `this_computer` | Signer role assigned when `public-key` is a raw PEM file (`this_computer`, `customer_key`, `openadapt_cloud`, `public_log`, `demo`). |
| `openings` | No | `""` | Optional path to a `receipt.private.json` openings sidecar. |
| `python-version` | No | `3.12` | Python version used to run verification. |
| `install-mode` | No | `auto` | `auto` (local `packages/types` if present, else PyPI), `pypi`, or `skip`. |

\* At least one of `public-key` or `keys` must be supplied; verification fails closed if no trusted key is configured.

### Outputs

| Output | Description |
| --- | --- |
| `verified` | `'true'` when offline verification passes, `'false'` otherwise. |
| `receipt-id` | Verified run/receipt identifier (empty on failure). |
| `outcome` | Recorded run outcome (`VERIFIED`, `STOPPED`, etc.; empty on failure). |
| `reason` | Empty on success, or the fail-closed refusal reason code on failure. |

### Example Step Summary

> ### ✅ Verified by OpenAdapt Seal
>
> | Field | Value |
> | --- | --- |
> | **Status** | Verified offline (Ed25519) |
> | **Receipt ID** | `run_2026_11_22_001` |
> | **Run Outcome** | `VERIFIED` |
> | **Ed25519 Signature** | Signed by customer key |
> | **Offline Verification** | `openadapt verify path/to/receipt.json` |
