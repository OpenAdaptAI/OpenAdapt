"""Receipt builders shared by the reward adapter tests.

Everything is signed by ``DevelopmentSigner``; nothing here is an issuer.
"""

from __future__ import annotations

from openadapt_types.reward import (
    RewardCertificateV1,
    RewardContractV1,
    RewardEvidenceReceiptV1,
    RewardOutcomeV1,
)

from openadapt_evals.reward.devsigner import DevelopmentSigner, sha256_digest

SIGNER = DevelopmentSigner(b"tests.reward_fixtures")
POLICY_CHECKPOINT = "policy.checkpoint.test-0001"
ISSUED_AT = "2026-09-01T00:00:00Z"


def contract() -> RewardContractV1:
    digest = sha256_digest(b"tests.reward_fixtures")
    return RewardContractV1.model_validate(
        {
            "contract_id": "reward.contract.test-0001",
            "contract_version": "reward.contract.test-0001.v1",
            "task_id": "task.test.0001",
            "task_digest": digest,
            "environment_id": "environment.test.0001",
            "environment_digest": digest,
            "required_effect_contract_digest": digest,
            "forbidden_effect_contract_digest": digest,
            "oracle": {
                "channel": "db",
                "identity_keys": ["record_id"],
                "oracle_contract_digest": digest,
            },
            "components": [{"name": "terminal_effect", "weight": 1.0}],
            "certificate_policy": {
                "epsilon": 0.05,
                "delta": 0.05,
                "threshold": 1.0,
                "calibration_corpus_digest": digest,
                "expiry_policy_updates": 10,
            },
        }
    )


CONTRACT = contract()


def certificate(
    *, issued_at_policy_update: int = 0, expiry_policy_updates: int = 10
) -> RewardCertificateV1:
    return SIGNER.issue_certificate(
        CONTRACT,
        certificate_id="reward.certificate.test-0001",
        checker_configuration_digest=sha256_digest(b"checker"),
        issued_at_policy_update=issued_at_policy_update,
        expiry_policy_updates=expiry_policy_updates,
        issued_at=ISSUED_AT,
    )


CERTIFICATE = certificate()


def receipt(
    episode_id: str,
    outcome: RewardOutcomeV1 | str,
    *,
    tier: int = 2,
    policy_update: int = 3,
    certificate: RewardCertificateV1 | None = CERTIFICATE,
    contract: RewardContractV1 = CONTRACT,
) -> RewardEvidenceReceiptV1:
    return SIGNER.issue_receipt(
        contract=contract,
        receipt_id=f"receipt.{episode_id}",
        episode_id=episode_id,
        policy_checkpoint_id=POLICY_CHECKPOINT,
        policy_update=policy_update,
        oracle_tier=tier,
        outcome=RewardOutcomeV1(outcome),
        evidence_digest=sha256_digest(episode_id.encode()),
        nonce=f"nonce.{episode_id}",
        issued_at=ISSUED_AT,
        certificate=certificate if tier >= 2 else None,
    )


def receipts_by_episode(
    outcomes: dict[str, RewardOutcomeV1 | str], **kwargs: object
) -> dict[str, RewardEvidenceReceiptV1]:
    return {episode: receipt(episode, outcome, **kwargs) for episode, outcome in outcomes.items()}


def identity_for(episode_id: str) -> dict[str, str]:
    """The oracle identity for one episode, keyed as ``CONTRACT.oracle.identity_keys``."""

    return {"record_id": f"rec.{episode_id}"}


def identities_for(episode_ids: list[str]) -> list[dict[str, str]]:
    """The ``oracle_identity`` dataset column for a batch."""

    return [identity_for(item) for item in episode_ids]
