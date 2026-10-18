"""Test helpers: a person-signed approver grant in a temporary key directory.

Made-up ids only. The signer key is generated per test and registered as a
``policy_signer``, as ``openadapt-flow jit register`` would.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openadapt_flow.approval_mode.approver import ApproverPolicyV1
from openadapt_flow.approval_mode.item_approval import ItemApprovalStore
from openadapt_flow.approval_mode.signed_policy import (
    ApproverGrant,
    SignedApproverGrant,
    sign_grant,
)
from openadapt_flow.jit.authority import public_hex, register_principal

SIGNER = "person-0001"
DELEGATES = ("agent:approver-bot", "system:acme")
_KEYS: dict[Path, Ed25519PrivateKey] = {}


def key_dir(tmp_path: Path) -> Path:
    return tmp_path / "keys"


def signer_key(tmp_path: Path) -> Ed25519PrivateKey:
    directory = key_dir(tmp_path)
    key = _KEYS.get(directory)
    if key is None:
        key = Ed25519PrivateKey.generate()
        register_principal(
            directory,
            role="policy_signer",
            principal_id=SIGNER,
            public_key_hex=public_hex(key),
        )
        _KEYS[directory] = key
    return key


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def make_grant(
    policy: ApproverPolicyV1,
    *,
    delegates: tuple[str, ...] = DELEGATES,
    workflows: tuple[str, ...] = (),
    signed_at: Optional[datetime] = None,
    days: int = 30,
) -> ApproverGrant:
    start = signed_at or datetime.now(timezone.utc) - timedelta(minutes=1)
    return ApproverGrant(
        grant_id="grant-0001",
        signer=SIGNER,
        signed_at=_iso(start),
        expires_at=_iso(start + timedelta(days=days)),
        approver_policy=policy,
        delegates=delegates,
        workflows=workflows,
    )


def signed_grant(
    tmp_path: Path, policy: ApproverPolicyV1, **kwargs
) -> SignedApproverGrant:
    return sign_grant(make_grant(policy, **kwargs), signer_key(tmp_path))


def grant_store(
    tmp_path: Path,
    policy: ApproverPolicyV1,
    *,
    run: str = "run",
    initiator: Optional[str] = None,
    cloud_keys: Optional[dict[str, str]] = None,
    **kwargs,
) -> ItemApprovalStore:
    store = ItemApprovalStore(
        tmp_path / run, key_dir=key_dir(tmp_path), cloud_keys=cloud_keys or {}
    )
    store.write_grant(signed_grant(tmp_path, policy, **kwargs))
    store.write_initiator(initiator)
    return store
