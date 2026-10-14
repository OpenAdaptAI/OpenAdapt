"""Deterministic ed25519 signer for tests and the reward proof.

This is not an issuer. The flow reward worker signs real receipts with a key
the trainer does not hold. This module lets a test or the proof harness
build receipts and certificates that satisfy the ``openadapt-types`` contract
without a network, and lets a reader verify them against the public key
recorded beside the output.

Every certificate issued here is self-signed and synthetic in scope.
"""

from __future__ import annotations

import hashlib
from base64 import b64decode, b64encode
from collections.abc import Mapping
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from openadapt_types.process_capability import canonical_json_bytes
from openadapt_types.reward import (
    DEFAULT_REWARD_SCORING,
    REWARD_CERTIFIED_MINIMUM_TIER,
    RewardCertificateStateV1,
    RewardCertificateV1,
    RewardContractV1,
    RewardEvidenceReceiptV1,
    RewardOutcomeV1,
    RewardScoringClassV1,
    RewardScoringPolicyV1,
    RewardUncertaintyStateV1,
    certificate_state,
)

SYNTHETIC_SCOPE = "synthetic"
SELF_SIGNED = "self_signed"


def sha256_digest(payload: bytes | Mapping[str, Any]) -> str:
    """``sha256:<hex>`` over bytes or over the canonical JSON of a mapping."""

    data = payload if isinstance(payload, bytes) else canonical_json_bytes(payload)
    return "sha256:" + hashlib.sha256(data).hexdigest()


class DevelopmentSigner:
    """Sign reward contracts' payloads with a key derived from a seed."""

    def __init__(self, seed_material: bytes, key_id: str = "dev.key.synthetic-proof") -> None:
        private_bytes = hashlib.sha256(
            b"openadapt-evals.reward.devsigner:" + seed_material
        ).digest()
        self._key = Ed25519PrivateKey.from_private_bytes(private_bytes)
        self.key_id = key_id

    @classmethod
    def from_seed(cls, seed: int, key_id: str = "dev.key.synthetic-proof") -> DevelopmentSigner:
        return cls(str(int(seed)).encode("ascii"), key_id=key_id)

    def public_key_bytes(self) -> bytes:
        from cryptography.hazmat.primitives import serialization

        return self._key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )

    def sign(self, unsigned_payload: Mapping[str, Any]) -> str:
        signature = self._key.sign(canonical_json_bytes(unsigned_payload))
        return b64encode(signature).decode("ascii")

    def issue_certificate(
        self,
        contract: RewardContractV1,
        *,
        certificate_id: str,
        checker_configuration_digest: str,
        issued_at_policy_update: int,
        issued_at: str,
        expiry_policy_updates: int | None = None,
    ) -> RewardCertificateV1:
        """Issue a self-signed, synthetic-scope certificate for ``contract``.

        The scope and the issuer are not parameters. This signer holds a key
        derived from a seed, and nobody verifies that key, so a certificate
        from here can honestly claim one thing: someone computed a bound on a
        synthetic corpus. Passing ``issuer="organization"`` used to buy a
        receipt reading ``certified: true, calibration_scope: production``
        with no worker, no oracle, and no read. There is no parameter to pass
        now, and ``openadapt-types`` has no enum member to accept.

        Every field except the identifiers is copied from
        ``contract.certificate_policy``, so the certificate satisfies the
        contract that asked for it.
        """

        policy = contract.certificate_policy
        payload: dict[str, Any] = {
            "certificate_id": certificate_id,
            "reward_contract_digest": contract.digest,
            "checker_configuration_digest": checker_configuration_digest,
            "epsilon": policy.epsilon,
            "delta": policy.delta,
            "threshold": policy.threshold,
            "calibration_corpus_digest": policy.calibration_corpus_digest,
            "issued_at_policy_update": issued_at_policy_update,
            "expiry_policy_updates": expiry_policy_updates or policy.expiry_policy_updates,
            "issued_at": issued_at,
            "calibration_scope": SYNTHETIC_SCOPE,
            "issuer": SELF_SIGNED,
            "issuer_key_id": self.key_id,
        }
        unsigned = RewardCertificateV1.model_validate({**payload, "signature": "A" * 86 + "=="})
        payload["signature"] = self.sign(unsigned.unsigned_payload())
        return RewardCertificateV1.model_validate(payload)

    def issue_receipt(
        self,
        *,
        contract: RewardContractV1,
        receipt_id: str,
        episode_id: str,
        policy_checkpoint_id: str,
        policy_update: int,
        oracle_tier: int,
        outcome: RewardOutcomeV1,
        evidence_digest: str,
        nonce: str,
        issued_at: str,
        certificate: RewardCertificateV1 | None = None,
        uncertainty: RewardUncertaintyStateV1 | None = None,
        scoring: RewardScoringPolicyV1 = DEFAULT_REWARD_SCORING,
    ) -> RewardEvidenceReceiptV1:
        """Issue a receipt whose flags follow the contract's own rules.

        ``certified`` is true only at tier 2 or 3 with a certificate that is
        current at ``policy_update``, names this contract by digest, and
        clears ``contract.certificate_policy``. That last check is the one
        that was missing: a certificate whose measured epsilon was 0.248885
        against a contract demanding 0.05 produced ``certified`` anyway.

        The calibration scope and corpus digest come from that certificate; a
        receipt with no certificate carries neither. Unscored outcomes carry
        no scalar and no components. The caller cannot override any of this.
        """

        outcome = RewardOutcomeV1(outcome)
        tier = int(oracle_tier)
        development_only = tier < REWARD_CERTIFIED_MINIMUM_TIER
        state = certificate_state(certificate, policy_update)
        if (
            certificate is not None
            and certificate.reward_contract_digest != contract.digest
        ):
            raise ValueError(
                f"certificate {certificate.certificate_id} names reward contract "
                f"{certificate.reward_contract_digest}, not {contract.digest}"
            )
        certified = (
            not development_only
            and certificate is not None
            and state is RewardCertificateStateV1.CURRENT
            and certificate.satisfies(contract.certificate_policy)
        )
        scalar = scoring.scalar_for(outcome)
        unscored = scalar is None
        if uncertainty is None:
            if outcome is RewardOutcomeV1.RECONCILIATION_REQUIRED:
                uncertainty = RewardUncertaintyStateV1.EFFECT_UNCERTAIN
            elif outcome is RewardOutcomeV1.FAILED_PLATFORM:
                uncertainty = RewardUncertaintyStateV1.ORACLE_UNAVAILABLE
            else:
                uncertainty = RewardUncertaintyStateV1.NONE
        components: dict[str, float] = {}
        if not unscored:
            first = contract.component_names[0]
            components = {first: float(scalar)}
        payload: dict[str, Any] = {
            "receipt_id": receipt_id,
            "reward_contract_digest": contract.digest,
            "policy_checkpoint_id": policy_checkpoint_id,
            "policy_update": policy_update,
            "episode_id": episode_id,
            "oracle_tier": tier,
            "reward_outcome": outcome.value,
            "evidence_digest": evidence_digest,
            "reward_components": components,
            "scalar_reward": scalar,
            "certificate_id": certificate.certificate_id if certificate is not None else None,
            "certificate_digest": certificate.digest if certificate is not None else None,
            "certificate_state": state.value,
            "calibration_corpus_digest": (
                certificate.calibration_corpus_digest if certificate is not None else None
            ),
            "calibration_scope": (
                certificate.calibration_scope.value if certificate is not None else None
            ),
            "uncertainty": uncertainty.value,
            "certified": certified,
            "development_only": development_only,
            "issuer_key_id": self.key_id,
            "nonce": nonce,
            "issued_at": issued_at,
        }
        unsigned = RewardEvidenceReceiptV1.model_validate({**payload, "signature": "A" * 86 + "=="})
        assert unsigned.scoring_class is (
            RewardScoringClassV1.UNSCORED if unscored else unsigned.scoring_class
        )
        payload["signature"] = self.sign(unsigned.unsigned_payload())
        return RewardEvidenceReceiptV1.model_validate(payload)


def verify_signature(
    signed: RewardCertificateV1 | RewardEvidenceReceiptV1, public_key_bytes: bytes
) -> bool:
    """True when ``signed.signature`` verifies over its unsigned payload."""

    public_key = Ed25519PublicKey.from_public_bytes(public_key_bytes)
    try:
        public_key.verify(
            b64decode(signed.signature), canonical_json_bytes(signed.unsigned_payload())
        )
    except InvalidSignature:
        return False
    return True
