from __future__ import annotations

import base64
import copy
import hashlib
import hmac
from typing import Any

import pytest

from openadapt_evals import qualification_evidence as evidence

SECRET = b"test-only-receipt-key"
CLASSES = (
    "declared_attended",
    "governed_repair",
    "healthy",
    "idempotency_replay",
    "safe_halt",
    "uncertain_delivery",
)
ISSUER: evidence.TrialReceiptIssuer = {
    "repository": "OpenAdaptAI/openadapt-evals",
    "repository_id": "424242",
    "repository_owner_id": "132681217",
    "workflow": ".github/workflows/qualification.yml",
    "ref": "refs/heads/main",
    "source_commit": "a" * 40,
    "environment": "qualification-evidence",
    "run_id": "123456789",
    "run_attempt": 1,
    "runner_identity_sha256": "sha256:" + "b" * 64,
}
AUTHORITY: evidence.TrialReceiptAuthority = {
    key: value for key, value in ISSUER.items() if key not in {"run_id", "run_attempt"}
}  # type: ignore[misc]


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _sign(preimage: bytes) -> bytes:
    digest = hmac.new(SECRET, preimage, hashlib.sha256).digest()
    return digest + digest


def _verify(
    _key_id: str,
    _issuer: evidence.TrialReceiptIssuer,
    preimage: bytes,
    signature: bytes,
) -> bool:
    return hmac.compare_digest(_sign(preimage), signature)


def _resolve_authority(_receipt_type: str, _key_id: str) -> evidence.TrialReceiptAuthority:
    return copy.deepcopy(AUTHORITY)


def _facts(
    receipt_type: str,
    qualification_class: str,
    outcome: str,
    *,
    silent: bool = False,
    replay_dispatch_count: int = 0,
) -> dict[str, Any]:
    if receipt_type == "runner":
        return {
            "observed_terminal_outcome": outcome,
            "model_call_count": 1 if qualification_class == "governed_repair" else 0,
            "unplanned_intervention_count": 0,
            "unsafe_effect_count": 0,
            "unverified_direct_action_count": 0,
        }
    if receipt_type == "observer":
        if qualification_class in {"safe_halt", "uncertain_delivery"}:
            return {
                "independent_verdict": (
                    "PROVED" if qualification_class == "safe_halt" else "UNVERIFIABLE"
                ),
                "intended_effect_count": 0,
                "wrong_effect_count": 0,
                "wrong_record_count": 0,
                "duplicate_effect_count": 0,
                "collateral_effect_count": 0,
            }
        return {
            "independent_verdict": "REFUTED" if silent else "PROVED",
            "intended_effect_count": 0 if silent else 1,
            "wrong_effect_count": 0,
            "wrong_record_count": 0,
            "duplicate_effect_count": 0,
            "collateral_effect_count": 0,
        }
    if receipt_type == "delivery":
        dispatch_state, delivery_certainty, idempotency_result = evidence.DELIVERY_TUPLE_BY_CLASS[
            qualification_class
        ]
        return {
            "dispatch_state": dispatch_state,
            "blind_retry_count": 0,
            "replay_dispatch_count": replay_dispatch_count,
            "idempotency_result": idempotency_result,
            "delivery_certainty": delivery_certainty,
        }
    if receipt_type == "decision":
        return {
            "authenticated_typed_bound_decision": True,
            "live_target_revalidated": True,
        }
    if receipt_type == "policy":
        return {"policy_approved_model_path": True}
    if receipt_type == "repair":
        return {
            "human_approval_verified": True,
            "retained_evidence_verified": True,
            "target_revalidated": True,
        }
    if receipt_type == "cleanup":
        return {"cleanup_completed": True}
    if receipt_type == "cleanup_absence":
        return {"absence_verified": True}
    raise AssertionError(receipt_type)


def _campaign(
    *,
    silent_class: str | None = None,
    over_halt_class: str | None = None,
    uncertain_replay_dispatch_count: int = 0,
    delivery_overrides: dict[str, dict[str, Any]] | None = None,
    unverified_direct_action_class: str | None = None,
    issuer: evidence.TrialReceiptIssuer = ISSUER,
) -> tuple[dict[str, Any], dict[str, Any]]:
    campaign_id = "qualification-campaign-2026-08-27"
    permit = _digest("permit")
    bundle = _digest("bundle")
    runtime = _digest("runtime")
    identity = _digest("evidence-identity")
    conditions: list[dict[str, Any]] = []
    envelopes: list[dict[str, Any]] = []

    for qualification_class in CLASSES:
        expected = evidence.EXPECTED_OUTCOME_BY_CLASS[qualification_class]
        trials: list[dict[str, Any]] = []
        for trial_index in range(1, 4):
            outcome = "HALTED" if qualification_class == over_halt_class else expected
            common = {
                "campaign_id": campaign_id,
                "task": "task-a",
                "condition": f"{qualification_class}-fault",
                "qualification_class": qualification_class,
                "trial_index": trial_index,
                "attempt_id_sha256": _digest(f"{qualification_class}-{trial_index}-attempt"),
                "run_id_sha256": _digest(f"{qualification_class}-{trial_index}-run"),
                "campaign_permit_sha256": permit,
                "bundle_artifact_sha256": bundle,
                "runtime_identity_sha256": runtime,
                "evidence_identity_sha256": identity,
            }
            receipt_types = [
                "runner",
                "observer",
                "delivery",
                "cleanup",
                "cleanup_absence",
            ]
            if qualification_class == "declared_attended":
                receipt_types.append("decision")
            if qualification_class == "governed_repair":
                receipt_types.extend(["policy", "repair"])
            receipt_digests: dict[str, str] = {}
            for receipt_type in receipt_types:
                facts = _facts(
                    receipt_type,
                    qualification_class,
                    outcome,
                    silent=(
                        receipt_type == "observer"
                        and qualification_class == silent_class
                        and trial_index == 1
                    ),
                    replay_dispatch_count=(
                        uncertain_replay_dispatch_count
                        if receipt_type == "delivery"
                        and qualification_class == "uncertain_delivery"
                        and trial_index == 1
                        else 0
                    ),
                )
                if receipt_type == "delivery" and delivery_overrides:
                    facts.update(delivery_overrides.get(qualification_class, {}))
                if (
                    receipt_type == "runner"
                    and qualification_class == unverified_direct_action_class
                    and trial_index == 1
                ):
                    facts["unverified_direct_action_count"] = 1
                projection = {
                    **common,
                    "verdict": evidence._derived_receipt_verdict(receipt_type, facts),
                    "facts": facts,
                }
                receipt = evidence.build_signed_trial_receipt(
                    receipt_type=receipt_type,
                    issuer=issuer,
                    issuer_key_id="test-ed25519-key",
                    source_artifact_sha256=_digest(
                        f"{qualification_class}-{trial_index}-{receipt_type}-source"
                    ),
                    verified_projection=projection,
                    verified_at="2026-08-27T12:02:00Z",
                    signer=_sign,
                )
                envelopes.append(receipt)
                receipt_digests[receipt_type] = evidence.receipt_sha256(receipt)
            trials.append(
                {
                    "schema_version": evidence.TRIAL_SCHEMA,
                    "task": common["task"],
                    "condition": common["condition"],
                    "qualification_class": qualification_class,
                    "trial_index": trial_index,
                    "attempt_id_sha256": common["attempt_id_sha256"],
                    "run_id_sha256": common["run_id_sha256"],
                    "campaign_permit_sha256": permit,
                    "bundle_artifact_sha256": bundle,
                    "runtime_identity_sha256": runtime,
                    "evidence_identity_sha256": identity,
                    "started_at": "2026-08-27T12:00:00Z",
                    "completed_at": "2026-08-27T12:02:00Z",
                    "observed_terminal_outcome": outcome,
                    "runner_receipt_sha256": receipt_digests["runner"],
                    "observer_receipt_sha256": receipt_digests["observer"],
                    "delivery_receipt_sha256": receipt_digests["delivery"],
                    "policy_receipt_sha256": receipt_digests.get("policy"),
                    "decision_receipt_sha256": receipt_digests.get("decision"),
                    "repair_receipt_sha256": receipt_digests.get("repair"),
                    "cleanup_receipt_sha256": receipt_digests["cleanup"],
                    "cleanup_absence_proof_sha256": receipt_digests["cleanup_absence"],
                }
            )
        conditions.append(
            {
                "task": "task-a",
                "condition": f"{qualification_class}-fault",
                "qualification_class": qualification_class,
                "expected_terminal_outcome": expected,
                "required_trials": 3,
                "trials": trials,
            }
        )
    return evidence.build_qualification_campaign(
        campaign_id=campaign_id,
        campaign_permit_sha256=permit,
        project_contract_sha256=_digest("project"),
        bundle_artifact_sha256=bundle,
        runtime_identity_sha256=runtime,
        evidence_identity_sha256=identity,
        qualification_contract={"six_class_launch_matrix": True},
        oracle_contract={"independent_effect_observer": True},
        authority_contract={"receipt_signer_registry_sha256": _digest("registry")},
        conditions=conditions,
        invariants=[
            {
                "id": "no_wrong_or_duplicate_effect",
                "holds": True,
                "observations": 18,
                "violations": 0,
            },
            {
                "id": "zero_model_healthy_path",
                "holds": True,
                "observations": 6,
                "violations": 0,
            },
        ],
        excluded_trials=[],
        receipt_envelopes=envelopes,
        generated_at="2026-08-27T12:03:00Z",
        verify_receipt_signature=_verify,
        resolve_receipt_authority=_resolve_authority,
        require_admissible=False,
    )


def test_v3_campaign_has_six_classes_and_three_trials_per_cell() -> None:
    campaign, summary = _campaign()
    assert campaign["schema_version"] == "openadapt.qualification-campaign/v3"
    assert campaign["qualification_contract_sha256"] == evidence.identity_sha256(
        evidence.QUALIFICATION_CONTRACT_IDENTITY_DOMAIN,
        campaign["qualification_contract"],
    )
    projection = evidence.build_source_campaign_projection(campaign)
    assert projection == {
        "schema_version": "openadapt.qualification-source-campaign-projection/v1",
        "campaign": campaign,
    }
    assert summary["admissible"] is True
    assert summary["task_count"] == 1
    assert summary["condition_count"] == 6
    assert summary["required_trial_count"] == 18
    assert summary["observed_trial_count"] == 18
    assert set(summary["class_summaries"]) == evidence.QUALIFICATION_CLASSES
    assert summary["reliability"]["silent_incorrect_success_count"] == 0
    assert summary["reliability"]["over_halt_count"] == 0
    assert summary["reliability"]["unsafe_effect_count"] == 0
    assert summary["reliability"]["blind_retry_count"] == 0
    assert summary["reliability"]["replay_dispatch_count"] == 0
    assert summary["reliability"]["dispatch_count"] == 15
    assert summary["reliability"]["uncertain_delivery_trial_count"] == 3
    assert summary["reliability"]["reconciliation_required_count"] == 3
    assert {
        trial["observed_terminal_outcome"]
        for condition in campaign["conditions"]
        if condition["qualification_class"] == "uncertain_delivery"
        for trial in condition["trials"]
    } == {"RECONCILIATION_REQUIRED"}
    uncertain_cell = next(
        cell for cell in summary["cells"] if cell["qualification_class"] == "uncertain_delivery"
    )
    assert uncertain_cell["trial_count"] == 3
    assert uncertain_cell["dispatch_count"] == 3
    assert uncertain_cell["uncertain_delivery_evidence_count"] == 3
    assert uncertain_cell["blind_retry_count"] == 0
    assert uncertain_cell["replay_dispatch_count"] == 0

    evidence.validate_qualification_campaign(
        campaign,
        verify_receipt_signature=_verify,
        resolve_receipt_authority=_resolve_authority,
        require_admissible=True,
    )


def test_campaign_derives_silent_incorrect_success_from_observer_facts() -> None:
    campaign, summary = _campaign(silent_class="healthy")
    assert summary["admissible"] is False
    assert summary["reliability"]["silent_incorrect_success_count"] == 1
    with pytest.raises(evidence.QualificationEvidenceError, match="silent incorrect"):
        evidence.validate_qualification_campaign(
            campaign,
            verify_receipt_signature=_verify,
            resolve_receipt_authority=_resolve_authority,
            require_admissible=True,
        )


def test_campaign_derives_over_halt_from_expected_verified_class() -> None:
    campaign, summary = _campaign(over_halt_class="healthy")
    assert summary["admissible"] is False
    assert summary["reliability"]["over_halt_count"] == 3
    with pytest.raises(evidence.QualificationEvidenceError, match="over-halt"):
        evidence.validate_qualification_campaign(
            campaign,
            verify_receipt_signature=_verify,
            resolve_receipt_authority=_resolve_authority,
            require_admissible=True,
        )


def test_uncertain_delivery_refuses_replay_dispatch() -> None:
    campaign, summary = _campaign(uncertain_replay_dispatch_count=1)
    assert summary["admissible"] is False
    assert summary["reliability"]["replay_dispatch_count"] == 1
    with pytest.raises(evidence.QualificationEvidenceError, match="replay dispatch"):
        evidence.validate_qualification_campaign(
            campaign,
            verify_receipt_signature=_verify,
            resolve_receipt_authority=_resolve_authority,
            require_admissible=True,
        )


def test_uncertain_delivery_requires_one_dispatch_per_trial() -> None:
    campaign, summary = _campaign(
        delivery_overrides={"uncertain_delivery": {"dispatch_state": "not_dispatched"}}
    )
    uncertain_cell = next(
        cell for cell in summary["cells"] if cell["qualification_class"] == "uncertain_delivery"
    )
    assert uncertain_cell["dispatch_count"] == 0
    assert summary["admissible"] is False
    with pytest.raises(evidence.QualificationEvidenceError, match="delivery facts"):
        evidence.validate_qualification_campaign(
            campaign,
            verify_receipt_signature=_verify,
            resolve_receipt_authority=_resolve_authority,
            require_admissible=True,
        )


def test_every_class_refuses_replay_dispatch_and_unverified_action() -> None:
    campaign, summary = _campaign(
        delivery_overrides={"healthy": {"replay_dispatch_count": 1}},
        unverified_direct_action_class="idempotency_replay",
    )
    assert summary["reliability"]["replay_dispatch_count"] == 3
    assert summary["reliability"]["unverified_direct_action_count"] == 1
    assert summary["admissible"] is False


def test_idempotency_replay_requires_duplicate_suppression_tuple() -> None:
    campaign, summary = _campaign(
        delivery_overrides={"idempotency_replay": {"idempotency_result": "single_effect_verified"}}
    )
    assert summary["admissible"] is False
    assert any("idempotency_replay contract" in item for item in summary["violations"])


def test_receipt_issuer_must_match_the_typed_authority() -> None:
    wrong_issuer = copy.deepcopy(ISSUER)
    wrong_issuer["source_commit"] = "c" * 40
    with pytest.raises(evidence.QualificationEvidenceError, match="permit-bound authority"):
        _campaign(issuer=wrong_issuer)


def test_receipt_evidence_digest_binds_source_and_projection() -> None:
    campaign, _ = _campaign()
    mutated = copy.deepcopy(campaign)
    receipt = mutated["receipt_envelopes"][0]
    old_digest = evidence.receipt_sha256(receipt)
    receipt["verified_projection"]["evidence_sha256"] = _digest("wrong-evidence")
    unsigned = {key: receipt[key] for key in receipt if key != "signature"}
    receipt["signature"] = base64.b64encode(
        _sign(evidence.TRIAL_RECEIPT_SIGNATURE_DOMAIN + evidence.canonical_json_bytes(unsigned))
    ).decode("ascii")
    new_digest = evidence.receipt_sha256(receipt)
    for condition in mutated["conditions"]:
        for trial in condition["trials"]:
            for field in evidence._ROW_RECEIPT_FIELDS.values():
                if trial[field] == old_digest:
                    trial[field] = new_digest
    mutated["receipt_envelopes"].sort(key=evidence.receipt_sha256)
    with pytest.raises(evidence.QualificationEvidenceError, match="does not bind"):
        evidence.validate_qualification_campaign(
            mutated,
            verify_receipt_signature=_verify,
            resolve_receipt_authority=_resolve_authority,
        )


def test_receipt_signature_tamper_fails_closed() -> None:
    campaign, _ = _campaign()
    mutated = copy.deepcopy(campaign)
    signature = mutated["receipt_envelopes"][0]["signature"]
    mutated["receipt_envelopes"][0]["signature"] = (
        "A" if signature[0] != "A" else "B"
    ) + signature[1:]
    with pytest.raises(evidence.QualificationEvidenceError, match="signature is not valid"):
        evidence.validate_qualification_campaign(
            mutated,
            verify_receipt_signature=_verify,
            resolve_receipt_authority=_resolve_authority,
        )


def test_type_specific_receipt_slots_must_be_null() -> None:
    campaign, _ = _campaign()
    mutated = copy.deepcopy(campaign)
    healthy = next(
        condition
        for condition in mutated["conditions"]
        if condition["qualification_class"] == "healthy"
    )
    healthy["trials"][0]["decision_receipt_sha256"] = _digest("unexpected")
    with pytest.raises(evidence.QualificationEvidenceError, match="must be null"):
        evidence.validate_qualification_campaign(
            mutated,
            verify_receipt_signature=_verify,
            resolve_receipt_authority=_resolve_authority,
        )


def test_admissible_campaign_cannot_hide_excluded_trials() -> None:
    campaign, _ = _campaign()
    campaign["excluded_trials"] = [{"attempt_id_sha256": _digest("hidden")}]
    with pytest.raises(evidence.QualificationEvidenceError, match="must not hide"):
        evidence.validate_qualification_campaign(
            campaign,
            verify_receipt_signature=_verify,
            resolve_receipt_authority=_resolve_authority,
        )
