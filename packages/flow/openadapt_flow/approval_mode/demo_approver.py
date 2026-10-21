"""A throwaway approver key for demos that play a person on made-up data.

A person's answer to a save counts only when a key registered under
``approvers`` signs it (:mod:`.human_attestation`). A demo that simulates the
person (``guard_demo``, the task-ledger demo) needs such a key, so it makes one
here, in a key directory the demo created for itself, and points its own run
at that directory. Nothing outside the demo trusts that directory, and the key
lives only in memory.

Never use this for real work: there a person registers their key at a
terminal (``jit register --role approver``), in the key directory the
deployment's runs read.
"""

from __future__ import annotations

from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openadapt_flow.approval_mode.human_attestation import (
    ApproverSigner,
    AttestationMethod,
)


def demo_approver(
    key_dir: Path,
    principal_id: str = "demo-person",
    *,
    method: AttestationMethod = "desktop_key",
) -> ApproverSigner:
    """Register a new in-memory approver key in the demo's own ``key_dir``."""

    from openadapt_flow.jit.authority import public_hex, register_principal

    key = Ed25519PrivateKey.generate()
    register_principal(
        Path(key_dir),
        role="approver",
        principal_id=principal_id,
        public_key_hex=public_hex(key),
    )
    return ApproverSigner(principal_id=principal_id, method=method, key=key)


__all__ = ["demo_approver"]
