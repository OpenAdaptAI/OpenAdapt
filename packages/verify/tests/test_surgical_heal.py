"""Tests for 3-layer surgical selector healing and signed candidate patch generation."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openadapt_verify.surgical_heal import (
    CandidatePatchedProgram,
    ElementTarget,
    FleetPatchError,
    FleetSelectorPatch,
    PatchScope,
    SurgicalHealCandidate,
    SurgicalSelectorHealer,
    apply_fleet_patch,
    apply_fleet_patch_to_packs,
    compute_program_sha256,
)


def _sample_steps() -> list[dict[str, str]]:
    return [
        {
            "workflow_id": "wf_policy_quote_submit",
            "step_id": "step_applicant_name",
            "selector": "input#applicant-name",
        },
        {
            "workflow_id": "wf_policy_quote_submit",
            "step_id": "step_effective_date",
            "selector": "input.nw-eff-date-v1",
        },
    ]


def _sample_scope(
    steps: list[dict[str, str]] | None = None,
    *,
    tenant_id: str = "tenant_alpha",
    program_id: str = "prog_quote_v1",
    app_origin: str = "https://portal.example.com",
    workflow_id: str = "wf_policy_quote_submit",
) -> PatchScope:
    source_steps = steps if steps is not None else _sample_steps()
    return PatchScope(
        tenant_id=tenant_id,
        program_id=program_id,
        program_sha256=compute_program_sha256(source_steps),
        app_origin=app_origin,
        workflow_id=workflow_id,
    )


def _drifted_quote_target() -> ElementTarget:
    return ElementTarget(
        workflow_id="wf_policy_quote_submit",
        step_id="step_effective_date",
        old_selector="input.nw-eff-date-v1",
        aria_role="textbox",
        accessible_name="Policy Effective Date",
        form_section="Coverage Dates",
        input_type="date",
    )


def _drifted_portal_candidates() -> list[SurgicalHealCandidate]:
    return [
        SurgicalHealCandidate(
            selector="input[data-testid='coverage-effective-date-v2']",
            aria_role="textbox",
            accessible_name="Policy Effective Date",
            form_section="Coverage Dates",
            input_type="date",
        ),
        SurgicalHealCandidate(
            selector="input[data-testid='coverage-expiration-date-v2']",
            aria_role="textbox",
            accessible_name="Policy Expiration Date",
            form_section="Coverage Dates",
            input_type="date",
        ),
        SurgicalHealCandidate(
            selector="input[data-testid='applicant-dob-v2']",
            aria_role="textbox",
            accessible_name="Date of Birth",
            form_section="Applicant Information",
            input_type="date",
        ),
        # Hidden element with identical label must be ignored fail-closed.
        SurgicalHealCandidate(
            selector="input#hidden-effective-date",
            aria_role="textbox",
            accessible_name="Policy Effective Date",
            form_section="Coverage Dates",
            input_type="date",
            visible=False,
        ),
    ]


def test_identity_anchored_heal_emits_signed_candidate_pending_qualification() -> None:
    signing_key = Ed25519PrivateKey.generate()
    verify_key = signing_key.public_key()
    steps = _sample_steps()
    scope = _sample_scope(steps)
    target = _drifted_quote_target()
    candidates = _drifted_portal_candidates()
    healer = SurgicalSelectorHealer(min_score=0.75, min_margin=0.15)

    oracle_calls: list[str] = []

    def readback_oracle(cand: SurgicalHealCandidate) -> tuple[bool, str]:
        oracle_calls.append(cand.selector)
        return True, "a" * 64

    result = healer.heal_and_verify(
        target,
        candidates,
        scope=scope,
        signing_key=signing_key,
        signer_key_id="key-primary-2026",
        readback_oracle=readback_oracle,
        pre_action_identity_check=lambda _t, _c: True,
    )

    assert result.verdict == "HEALED"
    assert result.readback_verified is True
    assert result.selected is not None
    assert (
        result.selected.candidate.selector
        == "input[data-testid='coverage-effective-date-v2']"
    )
    assert result.selected.score == 1.0
    assert oracle_calls == ["input[data-testid='coverage-effective-date-v2']"]
    assert result.patch is not None
    assert result.patch.verify_signature(verify_key) is True
    assert result.patch.verify_signature({"key-primary-2026": verify_key}) is True

    roundtripped = FleetSelectorPatch.from_json(result.patch.to_json())
    assert roundtripped == result.patch
    assert roundtripped.verify_signature(verify_key) is True

    candidate_prog = apply_fleet_patch(
        steps,
        roundtripped,
        verify_key=verify_key,
        scope=scope,
    )
    assert isinstance(candidate_prog, CandidatePatchedProgram)
    assert candidate_prog.status == "PENDING_QUALIFICATION"
    assert candidate_prog.requires_re_qualification is True
    assert candidate_prog.requires_admission_approval is True
    assert (
        candidate_prog.candidate_program_sha256 != candidate_prog.source_program_sha256
    )
    # Original steps are never mutated in place.
    assert steps[1]["selector"] == "input.nw-eff-date-v1"
    assert (
        candidate_prog.steps[1]["selector"]
        == "input[data-testid='coverage-effective-date-v2']"
    )
    assert (
        candidate_prog.steps[1]["healed_by_patch_sha256"] == roundtripped.patch_sha256
    )


def test_near_miss_renamed_label_halts_instead_of_healing_wrong_field() -> None:
    # Adversarial case: "Patient last name" must NEVER heal onto "Patient first name"
    # or "Surname" even when role, section, and input type all match.
    target = ElementTarget(
        workflow_id="wf_intake",
        step_id="step_last_name",
        old_selector="#last_name",
        aria_role="textbox",
        accessible_name="Patient last name",
        form_section="Demographics",
        input_type="text",
    )
    drifted = [
        SurgicalHealCandidate(
            selector="#fname",
            aria_role="textbox",
            accessible_name="Patient first name",
            form_section="Demographics",
            input_type="text",
        ),
        SurgicalHealCandidate(
            selector="#surname",
            aria_role="textbox",
            accessible_name="Surname",
            form_section="Demographics",
            input_type="text",
        ),
    ]
    healer = SurgicalSelectorHealer(min_score=0.75, min_margin=0.15)
    result = healer.rank_and_select(target, drifted)

    assert result.verdict == "ABSTAIN_HALT"
    assert "identity anchor" in result.reason
    assert result.selected is None
    assert result.patch is None

    # Single wrong candidate also halts fail-closed.
    single_wrong = healer.rank_and_select(target, [drifted[0]])
    assert single_wrong.verdict == "ABSTAIN_HALT"
    assert single_wrong.selected is None


def test_abstain_halt_on_ambiguous_tie_candidates() -> None:
    signing_key = Ed25519PrivateKey.generate()
    target = _drifted_quote_target()
    scope = _sample_scope()
    tied_candidates = [
        SurgicalHealCandidate(
            selector="input#eff-date-primary",
            aria_role="textbox",
            accessible_name="Policy Effective Date",
            form_section="Coverage Dates",
            input_type="date",
        ),
        SurgicalHealCandidate(
            selector="input#eff-date-secondary",
            aria_role="textbox",
            accessible_name="Policy Effective Date",
            form_section="Coverage Dates",
            input_type="date",
        ),
    ]
    healer = SurgicalSelectorHealer(min_score=0.75, min_margin=0.15)
    oracle_called = False

    def readback_oracle(_cand: SurgicalHealCandidate) -> bool:
        nonlocal oracle_called
        oracle_called = True
        return True

    result = healer.heal_and_verify(
        target,
        tied_candidates,
        scope=scope,
        signing_key=signing_key,
        signer_key_id="key-1",
        readback_oracle=readback_oracle,
    )

    assert result.verdict == "ABSTAIN_HALT"
    assert "ambiguous" in result.reason
    assert result.margin == 0.0
    assert result.selected is None
    assert result.patch is None
    assert oracle_called is False


def test_pre_action_identity_check_and_readback_failures_withhold_patch() -> None:
    signing_key = Ed25519PrivateKey.generate()
    target = _drifted_quote_target()
    scope = _sample_scope()
    candidates = _drifted_portal_candidates()
    healer = SurgicalSelectorHealer(min_score=0.75, min_margin=0.15)

    oracle_called = False

    def oracle(_cand: SurgicalHealCandidate) -> bool:
        nonlocal oracle_called
        oracle_called = True
        return True

    pre_fail = healer.heal_and_verify(
        target,
        candidates,
        scope=scope,
        signing_key=signing_key,
        signer_key_id="key-1",
        readback_oracle=oracle,
        pre_action_identity_check=lambda _t, _c: False,
    )
    assert pre_fail.verdict == "ABSTAIN_HALT"
    assert "pre-action identity check" in pre_fail.reason
    assert pre_fail.patch is None
    assert oracle_called is False

    readback_fail = healer.heal_and_verify(
        target,
        candidates,
        scope=scope,
        signing_key=signing_key,
        signer_key_id="key-1",
        readback_oracle=lambda _cand: False,
    )
    assert readback_fail.verdict == "ABSTAIN_HALT"
    assert readback_fail.readback_verified is False
    assert readback_fail.patch is None
    assert "read-back" in readback_fail.reason


def test_forged_signature_string_bool_missing_workflow_and_cross_tenant_rejected() -> (
    None
):
    trusted_key = Ed25519PrivateKey.generate()
    trusted_pub = trusted_key.public_key()
    attacker_key = Ed25519PrivateKey.generate()

    steps_a = _sample_steps()
    scope_a = _sample_scope(steps_a, tenant_id="tenant_a")
    scope_b = _sample_scope(steps_a, tenant_id="tenant_b")

    valid_patch = FleetSelectorPatch.create(
        scope=scope_a,
        step_id="step_effective_date",
        old_selector="input.nw-eff-date-v1",
        new_selector="input[data-testid='coverage-effective-date-v2']",
        readback_verified=True,
        confidence=1.0,
        margin=0.4,
        signing_key=trusted_key,
        signer_key_id="trusted-key",
    )

    # 1. Forged patch with recomputed sha256 signed by attacker key -> refused.
    forged_patch = FleetSelectorPatch.create(
        scope=scope_a,
        step_id="step_effective_date",
        old_selector="input.nw-eff-date-v1",
        new_selector="#evil",
        readback_verified=True,
        confidence=1.0,
        margin=0.4,
        signing_key=attacker_key,
        signer_key_id="trusted-key",
    )
    assert forged_patch.verify_signature(trusted_pub) is False
    with pytest.raises(
        FleetPatchError, match="signature/integrity verification failed"
    ):
        apply_fleet_patch(steps_a, forged_patch, verify_key=trusted_pub, scope=scope_a)

    # 2. JSON string "false" for readback_verified -> refused at parse time.
    bad_json_dict = valid_patch.to_dict()
    bad_json_dict["readback_verified"] = "false"
    with pytest.raises(FleetPatchError, match="JSON boolean"):
        FleetSelectorPatch.from_json(json.dumps(bad_json_dict))

    # 3. Cross-tenant application -> refused.
    with pytest.raises(FleetPatchError, match="scope mismatch on tenant_id"):
        apply_fleet_patch_to_packs(
            {"pack_a": steps_a, "pack_b": steps_a},
            valid_patch,
            verify_key=trusted_pub,
            pack_scopes={"pack_a": scope_a, "pack_b": scope_b},
        )

    # 4. Step missing workflow_id -> refused (never skipped).
    missing_wf_steps = [
        {"step_id": "step_effective_date", "selector": "input.nw-eff-date-v1"}
    ]
    missing_wf_scope = _sample_scope(missing_wf_steps, tenant_id="tenant_a")
    missing_wf_patch = FleetSelectorPatch.create(
        scope=missing_wf_scope,
        step_id="step_effective_date",
        old_selector="input.nw-eff-date-v1",
        new_selector="input#v2",
        readback_verified=True,
        confidence=1.0,
        margin=0.5,
        signing_key=trusted_key,
        signer_key_id="trusted-key",
    )
    with pytest.raises(FleetPatchError, match="missing required workflow_id"):
        apply_fleet_patch(
            missing_wf_steps,
            missing_wf_patch,
            verify_key=trusted_pub,
            scope=missing_wf_scope,
        )

    # 5. Tampered selector with original signature -> refused.
    tampered = replace(valid_patch, new_selector="button#delete")
    assert tampered.verify_signature(trusted_pub) is False
    with pytest.raises(
        FleetPatchError, match="signature/integrity verification failed"
    ):
        apply_fleet_patch(steps_a, tampered, verify_key=trusted_pub, scope=scope_a)
