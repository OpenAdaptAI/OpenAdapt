"""Contract tests for the reward contract, certificate, and evidence receipt."""

from __future__ import annotations

import json
from importlib.resources import files

import pytest
from pydantic import ValidationError

from openadapt_types import (
    EffectStrengthV1,
    ExecuteEvidenceContractV1,
    ExecuteEvidenceReceiptV1,
    ExecuteTerminalOutcomeV1,
)
from openadapt_types.reward import (
    DEFAULT_REWARD_SCORING,
    REWARD_CERTIFICATE_SCHEMA,
    REWARD_CONTRACT_SCHEMA,
    REWARD_EVIDENCE_RECEIPT_SCHEMA,
    REWARD_SCORING_CLASS,
    UNSCORED_REWARD_OUTCOMES,
    RewardCalibrationScopeV1,
    RewardCertificateIssuerV1,
    RewardCertificateStateV1,
    RewardCertificateV1,
    RewardCertificationRefused,
    RewardContractV1,
    RewardEvidenceReceiptV1,
    RewardOutcomeV1,
    RewardScoringClassV1,
    RewardScoringPolicyV1,
    RewardUncertaintyStateV1,
    certificate_state,
    refuse_development_certification,
    score,
)

_DIGEST = "sha256:" + "a" * 64
_OTHER_DIGEST = "sha256:" + "b" * 64
_SIGNATURE = "A" * 86 + "=="
# Pinned once from the payload below. A change here means the canonical form
# changed, which breaks every digest a consumer has stored.
_PINNED_CONTRACT_DIGEST = (
    "sha256:4dd42acff78802d1815798dfe04b95fe27847b972d348dcdafa7bbabc5d6be15"
)


def _contract_payload() -> dict[str, object]:
    return {
        "schema_version": REWARD_CONTRACT_SCHEMA,
        "contract_id": "reward.contract.0001",
        "contract_version": "reward.contract.0001.v1",
        "task_id": "task.reference.0001",
        "task_digest": _DIGEST,
        "environment_id": "environment.local.0001",
        "environment_digest": _OTHER_DIGEST,
        "required_effect_contract_digest": _DIGEST,
        "forbidden_effect_contract_digest": _OTHER_DIGEST,
        "oracle": {
            "channel": "file",
            "identity_keys": ["record_id"],
            "oracle_contract_digest": _DIGEST,
        },
        "components": [
            {"name": "terminal_effect", "weight": 1.0},
            {"name": "halt_on_uncertainty", "weight": 0.25},
        ],
        "certificate_policy": {
            "epsilon": 0.0114,
            "delta": 0.05,
            "threshold": 0.5,
            "calibration_corpus_digest": _DIGEST,
            "expiry_policy_updates": 50,
        },
    }


def _contract() -> RewardContractV1:
    return RewardContractV1.model_validate(_contract_payload())


def _certificate_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": REWARD_CERTIFICATE_SCHEMA,
        "certificate_id": "reward.certificate.0001",
        "reward_contract_digest": _contract().digest,
        "checker_configuration_digest": _OTHER_DIGEST,
        "epsilon": 0.0114,
        "delta": 0.05,
        "threshold": 0.5,
        "calibration_corpus_digest": _DIGEST,
        "calibration_scope": "synthetic",
        "issued_at_policy_update": 100,
        "expiry_policy_updates": 50,
        "issued_at": "2026-09-01T12:00:00Z",
        "issuer": "self_signed",
        "issuer_key_id": "key.reference.0001",
        "signature_algorithm": "ed25519",
        "signature": _SIGNATURE,
    }
    payload.update(updates)
    return payload


def _certificate(**updates: object) -> RewardCertificateV1:
    return RewardCertificateV1.model_validate(_certificate_payload(**updates))


def _receipt_payload(**updates: object) -> dict[str, object]:
    certificate = _certificate()
    payload: dict[str, object] = {
        "schema_version": REWARD_EVIDENCE_RECEIPT_SCHEMA,
        "receipt_id": "reward.receipt.0001",
        "reward_contract_digest": _contract().digest,
        "policy_checkpoint_id": "policy.checkpoint.0120",
        "policy_update": 120,
        "episode_id": "episode.reference.0001",
        "oracle_tier": 2,
        "reward_outcome": "verified",
        "evidence_digest": _OTHER_DIGEST,
        "reward_components": {"terminal_effect": 1.0, "halt_on_uncertainty": 0.0},
        "scalar_reward": 1.0,
        "certificate_id": certificate.certificate_id,
        "certificate_digest": certificate.digest,
        "certificate_state": "current",
        "calibration_corpus_digest": certificate.calibration_corpus_digest,
        "calibration_scope": certificate.calibration_scope.value,
        "uncertainty": "none",
        "certified": True,
        "development_only": False,
        "issuer_key_id": "key.reference.0001",
        "nonce": "nonce.reward.receipt.0001",
        "issued_at": "2026-09-01T12:00:00Z",
        "signature_algorithm": "ed25519",
        "signature": _SIGNATURE,
    }
    payload.update(updates)
    return payload


def _receipt(**updates: object) -> RewardEvidenceReceiptV1:
    return RewardEvidenceReceiptV1.model_validate(_receipt_payload(**updates))


# --- contract ---------------------------------------------------------------


def test_contract_digest_is_canonical_and_pinned() -> None:
    contract = _contract()
    assert contract.digest == _PINNED_CONTRACT_DIGEST
    round_trip = RewardContractV1.model_validate(json.loads(contract.model_dump_json()))
    assert round_trip.digest == contract.digest

    reordered = _contract_payload()
    reordered["components"] = list(reversed(reordered["components"]))  # type: ignore[arg-type]
    assert RewardContractV1.model_validate(reordered).digest == contract.digest
    assert contract.component_names == ("halt_on_uncertainty", "terminal_effect")


def test_contract_refuses_extra_keys_and_duplicate_components() -> None:
    payload = _contract_payload()
    payload["execution_id"] = "execution.reference.0001"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RewardContractV1.model_validate(payload)

    payload = _contract_payload()
    payload["components"] = [
        {"name": "terminal_effect", "weight": 1.0},
        {"name": "terminal_effect", "weight": 0.5},
    ]
    with pytest.raises(ValidationError, match="unique"):
        RewardContractV1.model_validate(payload)


def test_contract_cannot_declare_uncertainty_as_zero() -> None:
    payload = _contract_payload()
    payload["scoring"] = {"uncertain_episodes": "zero"}
    with pytest.raises(ValidationError, match="uncertain_episodes"):
        RewardContractV1.model_validate(payload)

    payload["scoring"] = {"platform_failures": "zero"}
    with pytest.raises(ValidationError, match="platform_failures"):
        RewardContractV1.model_validate(payload)

    payload["scoring"] = {"halted_before_effect_reward": 0.5}
    with pytest.raises(ValidationError, match="halted_before_effect_reward"):
        RewardContractV1.model_validate(payload)


def test_contract_oracle_channel_sets_the_tier() -> None:
    assert _contract().oracle.tier == 2
    payload = _contract_payload()
    payload["oracle"]["channel"] = "ocr"  # type: ignore[index]
    assert RewardContractV1.model_validate(payload).oracle.tier == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("epsilon", 0.0),
        ("epsilon", 1.0),
        ("delta", 1.5),
        ("threshold", float("nan")),
        ("calibration_corpus_digest", "sha256:short"),
        ("expiry_policy_updates", 0),
    ],
)
def test_certificate_policy_bounds(field: str, value: object) -> None:
    payload = _contract_payload()
    payload["certificate_policy"][field] = value  # type: ignore[index]
    with pytest.raises(ValidationError):
        RewardContractV1.model_validate(payload)


# --- certificate ------------------------------------------------------------


def test_certificate_expires_by_policy_update() -> None:
    certificate = _certificate()
    assert certificate.expires_at_policy_update == 150
    assert certificate.state_at(99) is RewardCertificateStateV1.NOT_YET_VALID
    assert certificate.is_current(100)
    assert certificate.is_current(149)
    assert not certificate.is_current(150)
    assert certificate.state_at(150) is RewardCertificateStateV1.EXPIRED
    assert certificate_state(None, 120) is RewardCertificateStateV1.ABSENT
    assert certificate_state(certificate, 120) is RewardCertificateStateV1.CURRENT


def test_certificate_satisfies_the_contract_policy() -> None:
    contract = _contract()
    assert _certificate().satisfies(contract.certificate_policy)
    assert not _certificate(epsilon=0.02).satisfies(contract.certificate_policy)
    assert not _certificate(calibration_corpus_digest=_OTHER_DIGEST).satisfies(
        contract.certificate_policy
    )
    assert not _certificate(expiry_policy_updates=51).satisfies(
        contract.certificate_policy
    )


def test_certificate_is_closed_and_signed() -> None:
    payload = _certificate_payload()
    payload["revoked"] = False
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RewardCertificateV1.model_validate(payload)

    with pytest.raises(ValidationError, match="signature"):
        _certificate(signature="not base64" + "=" * 78)

    certificate = _certificate()
    assert "signature" not in certificate.unsigned_payload()
    assert certificate.digest.startswith("sha256:")


# --- scoring helper ---------------------------------------------------------


def test_every_outcome_has_exactly_one_scoring_class() -> None:
    assert set(REWARD_SCORING_CLASS) == set(RewardOutcomeV1)
    assert UNSCORED_REWARD_OUTCOMES == {
        RewardOutcomeV1.RECONCILIATION_REQUIRED,
        RewardOutcomeV1.FAILED_PLATFORM,
    }
    assert REWARD_SCORING_CLASS[RewardOutcomeV1.VERIFIED] is (
        RewardScoringClassV1.ADMITTED_POSITIVE
    )
    for outcome in (
        RewardOutcomeV1.HALTED_BEFORE_EFFECT,
        RewardOutcomeV1.REFUSED,
        RewardOutcomeV1.REJECTED_POLICY,
        RewardOutcomeV1.WRONG_EFFECT,
    ):
        assert REWARD_SCORING_CLASS[outcome] is RewardScoringClassV1.ZERO_OR_PENALTY


@pytest.mark.parametrize("outcome", sorted(UNSCORED_REWARD_OUTCOMES, key=str))
def test_unscored_outcomes_never_become_zero(outcome: RewardOutcomeV1) -> None:
    scalar, certified, development_only, refusals = score(
        outcome, 2, _certificate(), 120, contract=_contract()
    )
    assert scalar is None
    assert certified is True
    assert development_only is False
    assert DEFAULT_REWARD_SCORING.scalar_for(outcome) is None


def test_verified_yields_the_admitted_positive_reward() -> None:
    scalar, certified, development_only, refusals = score(
        RewardOutcomeV1.VERIFIED, 3, _certificate(), 120, contract=_contract()
    )
    assert scalar == 1.0
    assert certified and not development_only
    assert refusals == ()

    custom = _contract_payload()
    custom["scoring"] = {"verified_reward": 2.5, "wrong_effect_reward": -3.0}
    contract = RewardContractV1.model_validate(custom)
    assert score(RewardOutcomeV1.VERIFIED, 2, None, 0, contract=contract).scalar == 2.5
    assert (
        score(RewardOutcomeV1.WRONG_EFFECT, 2, None, 0, contract=contract).scalar == -3.0
    )


def test_halt_and_rejection_yield_zero_or_declared_penalty() -> None:
    plain = _contract()
    assert (
        score(RewardOutcomeV1.HALTED_BEFORE_EFFECT, 2, None, 0, contract=plain).scalar
        == 0.0
    )
    assert score(RewardOutcomeV1.REJECTED_POLICY, 2, None, 0, contract=plain).scalar == 0.0
    assert score(RewardOutcomeV1.REFUSED, 2, None, 0, contract=plain).scalar == 0.0
    payload = _contract_payload()
    payload["scoring"] = {
        "halted_before_effect_reward": -0.1,
        "rejected_policy_reward": -0.5,
    }
    penalised = RewardContractV1.model_validate(payload)
    assert (
        score(
            RewardOutcomeV1.HALTED_BEFORE_EFFECT, 2, None, 0, contract=penalised
        ).scalar
        == -0.1
    )
    assert (
        score(RewardOutcomeV1.REJECTED_POLICY, 2, None, 0, contract=penalised).scalar
        == -0.5
    )


def test_tier_zero_and_one_are_development_only_and_never_certified() -> None:
    certificate = _certificate()
    for tier in (0, 1):
        scalar, certified, development_only, _ = score(
            RewardOutcomeV1.VERIFIED, tier, certificate, 120, contract=_contract()
        )
        assert scalar == 1.0
        assert certified is False
        assert development_only is True
        with pytest.raises(RewardCertificationRefused, match="oracle tier 2 or 3"):
            refuse_development_certification(tier)
    refuse_development_certification(2)


def test_expired_or_absent_certificate_is_not_certified() -> None:
    certificate = _certificate()
    contract = _contract()
    expired = score(RewardOutcomeV1.VERIFIED, 2, certificate, 150, contract=contract)
    assert expired.certified is False
    assert "expired" in expired.certification_refusals[0]
    early = score(RewardOutcomeV1.VERIFIED, 2, certificate, 99, contract=contract)
    assert early.certified is False
    assert "not_yet_valid" in early.certification_refusals[0]
    absent = score(RewardOutcomeV1.VERIFIED, 2, None, 120, contract=contract)
    assert absent.certified is False
    assert absent.certification_refusals == ("no reward certificate was presented",)
    with pytest.raises(ValueError, match="non-negative"):
        score(RewardOutcomeV1.VERIFIED, 2, certificate, -1, contract=contract)


# --- receipt ----------------------------------------------------------------


def test_receipt_round_trips_and_refuses_extra_keys() -> None:
    receipt = _receipt()
    assert receipt.scoring_class is RewardScoringClassV1.ADMITTED_POSITIVE
    assert receipt.digest == RewardEvidenceReceiptV1.model_validate(
        json.loads(receipt.model_dump_json())
    ).digest
    assert "signature" not in receipt.unsigned_payload()

    payload = _receipt_payload()
    payload["workflow_digest"] = _DIGEST
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RewardEvidenceReceiptV1.model_validate(payload)


def test_receipt_refuses_certified_at_tier_zero_or_one() -> None:
    for tier in (0, 1):
        with pytest.raises(ValidationError, match="oracle tier 2 or 3"):
            _receipt(oracle_tier=tier, development_only=True, certified=True)
        development = _receipt(oracle_tier=tier, development_only=True, certified=False)
        assert development.development_only is True
        assert development.certified is False

    with pytest.raises(ValidationError, match="development_only"):
        _receipt(oracle_tier=0, development_only=False, certified=False)
    with pytest.raises(ValidationError, match="development_only"):
        _receipt(oracle_tier=2, development_only=True, certified=False)


def test_receipt_certified_requires_a_current_referenced_certificate() -> None:
    for state in ("expired", "not_yet_valid"):
        with pytest.raises(ValidationError, match="current certificate"):
            _receipt(certificate_state=state, certified=True)
        _receipt(certificate_state=state, certified=False)

    with pytest.raises(ValidationError, match="current certificate"):
        _receipt(
            certificate_state="absent",
            certificate_id=None,
            certificate_digest=None,
            calibration_corpus_digest=None,
            calibration_scope=None,
            certified=True,
        )
    with pytest.raises(ValidationError, match="absent certificate"):
        _receipt(certificate_state="absent", certified=False)
    with pytest.raises(ValidationError, match="requires id, digest, corpus digest, and scope"):
        _receipt(certificate_digest=None)


@pytest.mark.parametrize("outcome", sorted(UNSCORED_REWARD_OUTCOMES, key=str))
def test_receipt_unscored_outcome_carries_no_scalar(outcome: RewardOutcomeV1) -> None:
    uncertainty = (
        "delivery_uncertain"
        if outcome is RewardOutcomeV1.RECONCILIATION_REQUIRED
        else "none"
    )
    with pytest.raises(ValidationError, match="unscored"):
        _receipt(reward_outcome=outcome.value, scalar_reward=0.0, uncertainty=uncertainty)
    receipt = _receipt(
        reward_outcome=outcome.value,
        scalar_reward=None,
        reward_components={},
        uncertainty=uncertainty,
    )
    assert receipt.scalar_reward is None
    assert receipt.scoring_class is RewardScoringClassV1.UNSCORED


def test_receipt_reconciliation_requires_uncertainty_and_verified_forbids_it() -> None:
    with pytest.raises(ValidationError, match="uncertainty state"):
        _receipt(
            reward_outcome="reconciliation_required",
            scalar_reward=None,
            reward_components={},
            uncertainty="none",
        )
    with pytest.raises(ValidationError, match="cannot carry uncertainty"):
        _receipt(uncertainty="effect_uncertain")


def test_receipt_scalar_sign_matches_the_scoring_class() -> None:
    with pytest.raises(ValidationError, match="must be positive"):
        _receipt(scalar_reward=0.0)
    with pytest.raises(ValidationError, match="zero or a declared penalty"):
        _receipt(reward_outcome="wrong_effect", scalar_reward=0.5)
    halted = _receipt(reward_outcome="halted_before_effect", scalar_reward=0.0)
    assert halted.scalar_reward == 0.0
    with pytest.raises(ValidationError, match="requires a scalar"):
        _receipt(reward_outcome="halted_before_effect", scalar_reward=None)
    with pytest.raises(ValidationError, match="at least one reward component"):
        _receipt(reward_components={})


def test_receipt_components_are_named_and_finite() -> None:
    with pytest.raises(ValidationError, match="invalid name"):
        _receipt(reward_components={"1bad": 1.0})
    with pytest.raises(ValidationError):
        _receipt(reward_components={"terminal_effect": float("inf")})
    receipt = _receipt(reward_components={"z": 1.0, "a": 0.5})
    assert list(receipt.reward_components) == ["a", "z"]


def test_receipt_uncertainty_states_are_closed() -> None:
    assert {item.value for item in RewardUncertaintyStateV1} == {
        "none",
        "delivery_uncertain",
        "effect_uncertain",
        "oracle_unavailable",
    }
    with pytest.raises(ValidationError):
        _receipt(uncertainty="maybe")


# --- calibration scope ------------------------------------------------------


def test_production_scope_is_unrepresentable() -> None:
    """The reviewer's reproduction: `issuer=organization` bought production scope."""

    for issuer in ("self_signed", "organization"):
        with pytest.raises(ValidationError, match="calibration_scope"):
            _certificate(issuer=issuer, calibration_scope="production")
    assert {item.value for item in RewardCalibrationScopeV1} == {"synthetic"}

    synthetic = _certificate(issuer="self_signed", calibration_scope="synthetic")
    assert synthetic.issuer is RewardCertificateIssuerV1.SELF_SIGNED
    assert synthetic.calibration_scope is RewardCalibrationScopeV1.SYNTHETIC


def test_an_unverifiable_issuer_identity_is_unrepresentable() -> None:
    with pytest.raises(ValidationError, match="issuer"):
        _certificate(issuer="organization")
    assert {item.value for item in RewardCertificateIssuerV1} == {"self_signed"}


def test_no_receipt_claims_a_production_scope() -> None:
    with pytest.raises(ValidationError, match="calibration_scope"):
        _receipt(calibration_scope="production")
    assert not hasattr(RewardEvidenceReceiptV1, "production_certified")


def test_certified_requires_corpus_digest_and_stated_scope() -> None:
    with pytest.raises(ValidationError, match="stated calibration scope"):
        _receipt(calibration_scope=None, certified=True)
    with pytest.raises(ValidationError, match="calibration corpus digest"):
        _receipt(calibration_corpus_digest=None, certified=True)
    with pytest.raises(ValidationError, match="corpus digest, and scope"):
        _receipt(calibration_scope=None, certified=False)

    receipt = _receipt()
    assert receipt.certified is True
    assert receipt.calibration_scope is RewardCalibrationScopeV1.SYNTHETIC

    scored = score(RewardOutcomeV1.VERIFIED, 2, _certificate(), 120, contract=_contract())
    assert scored.certified is True
    assert _certificate().calibration_scope is RewardCalibrationScopeV1.SYNTHETIC


# --- the contract's own certificate policy is enforced ----------------------


def test_a_certificate_weaker_than_the_contract_is_not_certified() -> None:
    """The reviewer's reproduction: epsilon 0.248885 against a contract of 0.05."""

    contract = _contract()
    weak = _certificate(epsilon=0.248885)
    assert weak.satisfies(contract.certificate_policy) is False
    scored = score(RewardOutcomeV1.VERIFIED, 2, weak, 120, contract=contract)
    assert scored.certified is False
    assert scored.scalar == 1.0
    assert scored.certification_refusals == (
        "certificate epsilon 0.248885 exceeds the contract's 0.0114",
    )


def test_every_shortfall_against_the_contract_policy_is_named() -> None:
    contract = _contract()
    assert _certificate().unmet(contract.certificate_policy) == ()
    cases = {
        "delta": ({"delta": 0.5}, "certificate delta 0.5 exceeds"),
        "threshold": ({"threshold": 0.9}, "is not the contract's 0.5"),
        "corpus": (
            {"calibration_corpus_digest": _OTHER_DIGEST},
            "names a calibration corpus the contract does not",
        ),
        "expiry": ({"expiry_policy_updates": 51}, "certificate expiry 51 policy updates"),
    }
    for updates, fragment in cases.values():
        certificate = _certificate(**updates)
        reasons = certificate.unmet(contract.certificate_policy)
        assert any(fragment in reason for reason in reasons), reasons
        assert certificate.satisfies(contract.certificate_policy) is False
        assert (
            score(
                RewardOutcomeV1.VERIFIED, 2, certificate, 120, contract=contract
            ).certified
            is False
        )


def test_a_certificate_for_another_contract_is_not_certified() -> None:
    payload = _contract_payload()
    payload["task_id"] = "task.reference.0002"
    other = RewardContractV1.model_validate(payload)
    scored = score(RewardOutcomeV1.VERIFIED, 2, _certificate(), 120, contract=other)
    assert scored.certified is False
    assert "different reward contract" in scored.certification_refusals[0]


def test_a_receipt_rechecks_its_own_certified_flag() -> None:
    contract = _contract()
    assert _receipt().certification_refusals(contract, _certificate()) == ()

    weak = _certificate(epsilon=0.248885)
    hand_built = _receipt(certificate_digest=weak.digest)
    refusals = hand_built.certification_refusals(contract, weak)
    assert refusals == ("certificate epsilon 0.248885 exceeds the contract's 0.0114",)

    payload = _contract_payload()
    payload["task_id"] = "task.reference.0002"
    other = RewardContractV1.model_validate(payload)
    assert _receipt().certification_refusals(other, _certificate()) == (
        "the receipt names a different reward contract",
        "the certificate names a different reward contract",
    )
    assert _receipt().certification_refusals(contract, None) == (
        "the receipt references a certificate that was not given",
        "no reward certificate was presented",
    )


def test_the_package_offers_no_revocation_check() -> None:
    """The docstrings described revocation as existing.  Nothing implements it."""

    import openadapt_types.reward as module

    assert not [name for name in dir(module) if "revo" in name.lower()]
    assert not [
        name
        for name in RewardCertificateV1.model_fields
        if "revo" in name.lower()
    ]


# --- not an Execute Seal ----------------------------------------------------


def _execute_receipt() -> ExecuteEvidenceReceiptV1:
    return ExecuteEvidenceReceiptV1(
        receipt_id="receipt_12345678",
        execution_id="execution_12345678",
        workflow_digest=_DIGEST,
        workflow_version="workflow_20260901",
        qualification_id="qualification_12345678",
        environment_id="environment_12345678",
        runner_id="runner:hosted",
        nonce="nonce:execution_12345678",
        oracle_tier=2,
        outcome=ExecuteTerminalOutcomeV1.VERIFIED,
        contracts=ExecuteEvidenceContractV1(
            authorization_passed=True,
            identity_passed=True,
            postcondition_passed=True,
            effect_passed=True,
            minimum_effect_strength=EffectStrengthV1.INDEPENDENT_SYSTEM_OF_RECORD,
            observed_effect_strength=EffectStrengthV1.INDEPENDENT_SYSTEM_OF_RECORD,
            model_used=False,
            external_network_used=False,
        ),
        delivery_uncertain=False,
        evidence_digest=_OTHER_DIGEST,
        issued_at="2026-09-01T12:00:00Z",
    )


def test_reward_receipt_is_not_an_execute_seal() -> None:
    assert REWARD_EVIDENCE_RECEIPT_SCHEMA != ExecuteEvidenceReceiptV1.model_fields[
        "schema_version"
    ].default

    reward_fields = set(RewardEvidenceReceiptV1.model_fields)
    execute_fields = set(ExecuteEvidenceReceiptV1.model_fields)
    # Generic receipt plumbing only. No Seal-specific field is shared.
    assert reward_fields & execute_fields == {
        "schema_version",
        "receipt_id",
        "oracle_tier",
        "evidence_digest",
        "nonce",
        "issued_at",
    }
    for seal_only in (
        "execution_id",
        "workflow_digest",
        "workflow_version",
        "qualification_id",
        "runner_id",
        "contracts",
        "outcome",
        "delivery_uncertain",
        "compensation_effect_verified",
    ):
        assert seal_only not in reward_fields

    seal = _execute_receipt().model_dump(mode="json")
    with pytest.raises(ValidationError):
        RewardEvidenceReceiptV1.model_validate(seal)
    with pytest.raises(ValidationError):
        ExecuteEvidenceReceiptV1.model_validate(_receipt().model_dump(mode="json"))


@pytest.mark.parametrize(
    ("model", "filename"),
    [
        (RewardContractV1, "reward-contract-v1.json"),
        (RewardCertificateV1, "reward-certificate-v1.json"),
        (RewardEvidenceReceiptV1, "reward-evidence-receipt-v1.json"),
    ],
)
def test_packaged_schemas_match_models(model: object, filename: str) -> None:
    packaged = json.loads(
        files("openadapt_types.schemas").joinpath(filename).read_text(encoding="utf-8")
    )
    assert packaged == model.model_json_schema()
