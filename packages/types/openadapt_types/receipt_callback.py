"""Check a settled runner receipt before attaching it to a managed callback."""

from __future__ import annotations

from typing import Any, Mapping

from openadapt_types import receipt


def verified_receipt_metrics(
    envelope_bytes: bytes,
    *,
    pinned_signer: Mapping[str, str],
    report_jcs_sha256: str,
    outcome: str,
) -> dict[str, Any]:
    """Verify public bytes against the runner's pre-established public key.

    The caller supplies its final callback outcome in the receipt vocabulary
    and the RFC 8785 digest computed from its actual report. Only the DSSE
    envelope is eligible; private openings and public-card wrappers never
    belong in a runner callback.
    """
    if len(envelope_bytes) > 2 * receipt.MAX_PAYLOAD_BYTES:
        raise ValueError("receipt exceeds the callback size limit")
    if set(pinned_signer) != {"key_id", "public_key"}:
        raise ValueError("the runner pin must contain its key ID and public key")
    keys = receipt.parse_registry(
        {
            "schema": receipt.REGISTRY_SCHEMA,
            "keys": [{**pinned_signer, "role": "this_computer"}],
        }
    )
    envelope = receipt.loads_strict(envelope_bytes)
    if not isinstance(envelope, dict) or not receipt.is_receipt_envelope(envelope):
        raise ValueError("expected a runner-signed receipt envelope")
    checked = receipt.verify_receipt(envelope, keys)
    if not checked.ok or checked.statement is None:
        raise ValueError(f"receipt verification failed: {checked.reason}")
    if len(checked.signers) != 1 or checked.signers[0].key_id != keys[0].key_id:
        raise ValueError("expected one runner signature")
    if checked.outcome != outcome:
        raise ValueError("receipt outcome differs from the callback outcome")
    if not receipt.report_subject_match(
        checked.statement, canonical_sha256=report_jcs_sha256
    ):
        raise ValueError("receipt does not cover the callback's canonical report")
    return {
        "receipt_record": envelope,
        "receipt_record_signer": dict(pinned_signer),
    }
