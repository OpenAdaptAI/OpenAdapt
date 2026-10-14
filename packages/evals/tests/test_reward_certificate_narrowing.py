"""A certificate cannot buy a claim nobody checks.

Each test here drives a reproduction that succeeded against published
openadapt-evals 0.97.0.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from openadapt_types.reward import (
    RewardCalibrationScopeV1,
    RewardCertificateIssuerV1,
    RewardCertificateV1,
    RewardOutcomeV1,
)

from openadapt_evals.reward.devsigner import (
    SELF_SIGNED,
    SYNTHETIC_SCOPE,
    sha256_digest,
    verify_signature,
)
from openadapt_evals.reward.receipts import assess_receipt
from tests.reward_fixtures import CONTRACT, ISSUED_AT, POLICY_CHECKPOINT, SIGNER


def _certificate(**overrides: Any) -> RewardCertificateV1:
    """Mint a certificate with fields the signer would never choose itself."""

    policy = CONTRACT.certificate_policy
    payload: dict[str, Any] = {
        "certificate_id": "reward.certificate.narrowing-0001",
        "reward_contract_digest": CONTRACT.digest,
        "checker_configuration_digest": sha256_digest(b"checker"),
        "epsilon": policy.epsilon,
        "delta": policy.delta,
        "threshold": policy.threshold,
        "calibration_corpus_digest": policy.calibration_corpus_digest,
        "calibration_scope": SYNTHETIC_SCOPE,
        "issued_at_policy_update": 0,
        "expiry_policy_updates": policy.expiry_policy_updates,
        "issued_at": ISSUED_AT,
        "issuer": SELF_SIGNED,
        "issuer_key_id": SIGNER.key_id,
    }
    payload.update(overrides)
    unsigned = RewardCertificateV1.model_validate({**payload, "signature": "A" * 86 + "=="})
    payload["signature"] = SIGNER.sign(unsigned.unsigned_payload())
    return RewardCertificateV1.model_validate(payload)


def _receipt(certificate: RewardCertificateV1 | None, **kwargs: Any):
    return SIGNER.issue_receipt(
        contract=kwargs.pop("contract", CONTRACT),
        receipt_id="receipt.narrowing-0001",
        episode_id="episode.narrowing-0001",
        policy_checkpoint_id=POLICY_CHECKPOINT,
        policy_update=1,
        oracle_tier=2,
        outcome=RewardOutcomeV1.VERIFIED,
        evidence_digest=sha256_digest(b"evidence"),
        nonce="nonce.narrowing-0001",
        issued_at=ISSUED_AT,
        certificate=certificate,
        **kwargs,
    )


def _issue(**kwargs: Any) -> RewardCertificateV1:
    return SIGNER.issue_certificate(
        CONTRACT,
        certificate_id="reward.certificate.narrowing-0002",
        checker_configuration_digest=sha256_digest(b"checker"),
        issued_at_policy_update=0,
        issued_at=ISSUED_AT,
        **kwargs,
    )


def test_the_signer_takes_no_issuer_or_scope_argument() -> None:
    """`issuer="organization"` bought `certified: true, scope: production`."""

    with pytest.raises(TypeError, match="issuer"):
        _issue(issuer="organization")
    with pytest.raises(TypeError, match="calibration_scope"):
        _issue(calibration_scope="production")

    certificate = _issue()
    assert certificate.issuer is RewardCertificateIssuerV1.SELF_SIGNED
    assert certificate.calibration_scope is RewardCalibrationScopeV1.SYNTHETIC
    assert verify_signature(certificate, SIGNER.public_key_bytes())


def test_a_receipt_never_states_a_production_scope() -> None:
    receipt = _receipt(_issue())
    assert receipt.certified is True
    assert receipt.calibration_scope is RewardCalibrationScopeV1.SYNTHETIC
    assert receipt.model_dump(mode="json")["calibration_scope"] == "synthetic"


def test_a_certificate_weaker_than_the_contract_is_not_certified() -> None:
    """Measured epsilon 0.248885 against a contract demanding 0.05."""

    weak = _certificate(epsilon=0.248885)
    assert CONTRACT.certificate_policy.epsilon == 0.05
    assert weak.satisfies(CONTRACT.certificate_policy) is False

    receipt = _receipt(weak)
    assert receipt.certified is False
    assert receipt.scalar_reward == 1.0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("delta", 0.9),
        ("threshold", 0.25),
        ("calibration_corpus_digest", sha256_digest(b"another corpus")),
        ("expiry_policy_updates", 11),
    ],
)
def test_every_shortfall_stops_certification(field: str, value: Any) -> None:
    receipt = _receipt(_certificate(**{field: value}))
    assert receipt.certified is False


def test_a_certificate_for_another_contract_is_refused() -> None:
    other = CONTRACT.model_copy(update={"task_id": "task.test.0002"})
    with pytest.raises(ValueError, match="names reward contract"):
        _receipt(_issue(), contract=other)


def test_the_trainer_refuses_a_weak_certificate_it_can_measure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`assess_receipt` given the contract policy rechecks the bound itself."""

    weak = _certificate(epsilon=0.248885)
    # A receipt the worker built before this refusal existed.
    payload = _receipt(_issue()).model_dump(mode="json")
    payload["certificate_id"] = weak.certificate_id
    payload["certificate_digest"] = weak.digest
    unsigned = type(_receipt(_issue())).model_validate(
        {**payload, "signature": "A" * 86 + "=="}
    )
    payload["signature"] = SIGNER.sign(unsigned.unsigned_payload())
    stale = type(unsigned).model_validate(payload)
    assert stale.certified is True

    trusting = assess_receipt(
        stale,
        policy_update=1,
        expected_contract_digest=CONTRACT.digest,
        certificate=weak,
    )
    assert trusting.certified is True

    with caplog.at_level(logging.WARNING, logger="openadapt_evals.reward.receipts"):
        checked = assess_receipt(
            stale,
            policy_update=1,
            expected_contract_digest=CONTRACT.digest,
            certificate=weak,
            certificate_policy=CONTRACT.certificate_policy,
        )
    assert checked.certified is False
    assert checked.scalar == 1.0
    assert "weaker than the contract policy" in caplog.text
