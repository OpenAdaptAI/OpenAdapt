"""A throwaway approver key for demos that play a person on made-up data.

A person's answer to a save counts only when a key on the signed approver
roster signs it, and the run pins that roster's root
(:mod:`.approver_roster`). A demo that simulates the person (``guard_demo``,
the task-ledger demo) needs such a key, so it starts a roster here, in a key
directory the demo created for itself, with an in-memory key as its root, and
pins its own run to that root (:func:`demo_root`). Nothing outside the demo
trusts that directory or root.

Never use this for real work: there the first approver is set up in Desktop
behind Touch ID, or pinned in the deployment config.
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
    """Start a roster in the demo's own ``key_dir`` with an in-memory root key."""

    from openadapt_flow.approval_mode.approver_roster import file_key_signer, init_root
    from openadapt_flow.jit.authority import public_hex

    key = Ed25519PrivateKey.generate()
    init_root(
        Path(key_dir),
        principal=principal_id,
        public_key=public_hex(key),
        sign=file_key_signer(key),
    )
    return ApproverSigner(principal_id=principal_id, method=method, signing=key)


def demo_root(signer: ApproverSigner) -> str:
    """The root a demo pins: its own approver's public key."""

    from openadapt_flow.jit.authority import public_hex

    return public_hex(signer.signing)  # type: ignore[arg-type]


__all__ = ["demo_approver", "demo_root"]
